"""scripts/migrate_budget_hook.py against a scripted fake of az, git, func and HTTP.

No test here runs a real ``az``, ``func``, network call, or touches
~/.wattracker: every side effect goes through the injected runner/http fakes.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "migrate_budget_hook.py"

SUB = "11111111-2222-3333-4444-555555555555"
RG = "wattracker-rg"
APP = "wattracker-budget-hook"
BOOT = "wattrackerhook"
STORAGE = "wattrackerapp"
PID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
HOST = "wattracker-budget-hook-abc123.eastus2-01.azurewebsites.net"
READ_HOST = "wattracker-read.proudcoast-test.eastus2.azurecontainerapps.io"
SUBNET_ID = (
    f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.Network"
    "/virtualNetworks/wattracker-vnet/subnets/budget-hook-flex"
)
STORAGE_ID = f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.Storage/storageAccounts/{STORAGE}"

# Distinctive sentinels: none may ever reach argv, stdout/stderr or exception text.
TOKEN = "TOKEN-SENTINEL-9f2c7e41d0"
HOST_KEY = "HOSTKEY-SENTINEL-4b81aa07"
MASTER_KEY = "MASTERKEY-SENTINEL-c3d95e12"
SECRETS = (TOKEN, HOST_KEY, MASTER_KEY)

PARAMS = (
    "using './main.bicep'\n"
    "\n"
    "// Source: resource-group/portal.\n"
    "param location = 'eastus2'\n"
    f"param storageName = '{STORAGE}'\n"
    "// Source: Function App output (defaultHostName, with no scheme).\n"
    "param budgetHookHost = 'old-y1-host.azurewebsites.net'\n"
    "param budgetHookFunctionAppName = 'old-y1-name'\n"
    "param billingEmail = 'owner@example.invalid'\n"
    "param budgetHookPrincipalId = 'c2691b35-abcf-4b0c-bbb6-4fdbfc6e3798'  // old Y1 identity\n"
    "param readImage = 'ghcr.io/poopaskoopa/wattracker-cloud@sha256:" + "1" * 64 + "'\n"
    "param syncImage = 'ghcr.io/poopaskoopa/wattracker-cloud@sha256:" + "1" * 64 + "'\n"
    "param cloudServerSecret = readEnvironmentVariable('WATTRACKER_CLOUD_SERVER_SECRET')\n"
    "param operatorToken = readEnvironmentVariable('WATTRACKER_OPERATOR_TOKEN')\n"
    "param staticRepositoryToken = readEnvironmentVariable('WATTRACKER_STATIC_REPOSITORY_TOKEN', '')\n"
)
ENV = {
    "WATTRACKER_BUDGET_HOOK_TOKEN": TOKEN,
    "WATTRACKER_CLOUD_SERVER_SECRET": "server-secret-value",
    "WATTRACKER_OPERATOR_TOKEN": "operator-token-value",
}
NOW = 1_790_000_000.0


def _load():
    spec = importlib.util.spec_from_file_location("migrate_budget_hook_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve string annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mig():
    return _load()


def _result(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


class FakeWorld:
    """A scripted Azure/git/func. State changes as write commands run."""

    def __init__(self, repo: Path, mig, **state):
        self.repo = repo
        self.mig = mig
        self.calls: list[list[str]] = []
        self.cwds: list[Path | None] = []
        self.subnet = state.get("subnet", "ok")  # ok | missing | a dict override
        self.vnet = state.get("vnet", True)
        self.vnet_prefixes = state.get("vnet_prefixes", ["10.42.0.0/16"])
        self.boot_storage = state.get("boot_storage", True)
        self.app = state.get("app", "y1")  # y1 | flex | missing | flex-unintegrated
        self.identity = state.get("identity", self.app in ("flex", "y1"))
        self.tracked = state.get("tracked", False)
        self.deploy_rc = state.get("deploy_rc", 0)
        self.deploy_reaches_azure = state.get("deploy_reaches_azure", True)
        self.deployed = state.get("deployed", False)
        self.role_assigned = state.get("role_assigned", True)
        self.default_action = state.get("default_action", "Deny")
        self.ip_rules = state.get("ip_rules", [])
        self.settings_rc = state.get("settings_rc", 0)
        self.read_app = state.get("read_app", True)
        self.fail = state.get("fail", ())  # argv prefixes that exit 1
        self.settings_files: list[dict] = []

    # -- helpers -------------------------------------------------------
    def _has(self, argv, *words):
        return all(word in argv for word in words)

    def _json(self, argv, value, rc=0):
        return _result(argv, rc, json.dumps(value) if value is not None else "",
                       f"diagnostic noise {HOST_KEY} {TOKEN}")

    def _missing(self, argv):
        return _result(argv, 3, "", f"(ResourceNotFound) diagnostic {MASTER_KEY}")

    def _subnet(self):
        if isinstance(self.subnet, dict):
            return self.subnet
        return {
            "id": SUBNET_ID,
            "addressPrefix": "10.42.2.0/27",
            "provisioningState": "Succeeded",
            "delegations": [{"name": "budget-hook-flex", "serviceName": "Microsoft.App/environments"}],
            "serviceEndpoints": [{"service": "Microsoft.Storage", "locations": ["eastus2"]}],
        }

    def _app(self):
        if self.app == "y1":
            return {"name": APP, "kind": "functionapp,linux", "sku": "Dynamic",
                    "defaultHostName": "old-y1-host.azurewebsites.net"}
        integrated = SUBNET_ID if self.app == "flex" else None
        return {"name": APP, "kind": "functionapp,linux", "sku": "FlexConsumption",
                "functionAppConfig": {"runtime": {"name": "python"}},
                "defaultHostName": HOST, "virtualNetworkSubnetId": integrated}

    # -- dispatch -------------------------------------------------------
    def __call__(self, argv, *, cwd=None, capture=True):
        argv = list(argv)
        self.calls.append(argv)
        self.cwds.append(cwd)
        for prefix in self.fail:
            if argv[: len(prefix)] == list(prefix):
                return _result(argv, 1, f"stdout {HOST_KEY}", f"stderr {TOKEN} {MASTER_KEY}")
        if argv[0] == "git":
            sub = argv[3]
            if sub == "ls-files":
                return _result(argv, 0 if self.tracked else 1)
            if sub == "check-ignore":
                return _result(argv, 0)
            if sub == "branch":
                return _result(argv, 0, "main\n")
            if sub == "status":
                return _result(argv, 0, "")
            raise AssertionError(argv)
        if argv[0] == "func":
            assert cwd == self.repo / "build" / "azure-budget-hook"
            return _result(argv, 0, "Remote build succeeded")
        if argv[0] == "/venv/python":
            script = Path(argv[1]).name
            if script == "deploy_cloud.py":
                assert capture is False, "deploy_cloud.py must inherit stdio"
                if self.deploy_rc == 0 and self.deploy_reaches_azure:
                    self.deployed = True
                return _result(argv, self.deploy_rc)
            if script == "package_budget_hook.py":
                (self.repo / "build" / "azure-budget-hook").mkdir(parents=True)
                return _result(argv, 0, "staged\n")
            raise AssertionError(argv)
        assert argv[0] == "az", argv
        if argv[1:3] == ["account", "show"]:
            return self._json(argv, {"id": SUB, "name": "Azure subscription 1"})
        if argv[1:3] == ["group", "show"]:
            return self._json(argv, {"name": RG})
        if argv[1:3] == ["functionapp", "list-flexconsumption-locations"]:
            return self._json(argv, [{"name": "eastus2"}, {"name": "westus"}])
        if argv[1:3] == ["provider", "show"]:
            return self._json(argv, {"registrationState": "Registered"})
        if argv[1:4] == ["network", "vnet", "show"]:
            if not self.vnet:
                return self._missing(argv)
            return self._json(argv, {"addressSpace": {"addressPrefixes": self.vnet_prefixes}})
        if argv[1:5] == ["network", "vnet", "subnet", "show"]:
            if self.subnet == "missing":
                return self._missing(argv)
            return self._json(argv, self._subnet())
        if argv[1:3] == ["rest", "--method"]:
            url = argv[argv.index("--url") + 1]
            assert argv[3] == "put" and "/subnets/budget-hook-flex?" in url
            self.subnet = "ok"
            return self._json(argv, {})
        if argv[1:4] == ["storage", "account", "show"]:
            name = argv[argv.index("--name") + 1]
            if name == BOOT:
                return self._json(argv, {"name": BOOT}) if self.boot_storage else self._missing(argv)
            assert name == STORAGE
            vnet_rules = [{"virtualNetworkResourceId": "/x/aca-infrastructure", "action": "Allow"}]
            if self.deployed:
                vnet_rules.append({"virtualNetworkResourceId": SUBNET_ID.upper(), "action": "Allow"})
            return self._json(argv, {"id": STORAGE_ID, "networkRuleSet": {
                "defaultAction": self.default_action, "bypass": "None",
                "ipRules": self.ip_rules, "virtualNetworkRules": vnet_rules}})
        if argv[1:4] == ["storage", "account", "create"]:
            self.boot_storage = True
            return self._json(argv, {"name": BOOT})
        if argv[1:3] == ["functionapp", "show"]:
            return self._missing(argv) if self.app == "missing" else self._json(argv, self._app())
        if argv[1:3] == ["functionapp", "delete"]:
            self.app, self.identity = "missing", False
            return self._json(argv, None)
        if argv[1:3] == ["functionapp", "create"]:
            self.app = "flex"
            return self._json(argv, self._app())
        if argv[1:4] == ["functionapp", "identity", "show"]:
            return self._json(argv, {"principalId": PID} if self.identity else None)
        if argv[1:4] == ["functionapp", "identity", "assign"]:
            self.identity = True
            return self._json(argv, {"principalId": PID})
        if argv[1:4] == ["role", "assignment", "list"]:
            items = [{"principalId": "someone-else", "roleDefinitionName": "Reader"}]
            if self.deployed and self.role_assigned:
                items.append({"principalId": PID, "roleDefinitionName": "Wattracker Budget Hook Writer"})
            return self._json(argv, items)
        if argv[1:5] == ["functionapp", "config", "appsettings", "set"]:
            settings = argv[argv.index("--settings") + 1]
            assert settings.startswith("@")
            path = Path(settings[1:])
            self.settings_files.append({
                "path": path,
                "mode": stat.S_IMODE(path.stat().st_mode),
                "content": json.loads(path.read_text()),
            })
            return _result(argv, self.settings_rc, f"[{{\"value\": \"{TOKEN}\"}}]", f"err {TOKEN}")
        if argv[1:4] == ["functionapp", "keys", "list"]:
            return self._json(argv, {"functionKeys": {"default": HOST_KEY},
                                     "masterKey": MASTER_KEY, "systemKeys": {}})
        if argv[1:3] == ["containerapp", "show"]:
            assert argv[argv.index("--name") + 1] == "wattracker-read"
            if not self.read_app:
                return self._missing(argv)
            return self._json(argv, {"name": "wattracker-read", "properties": {
                "configuration": {"ingress": {"fqdn": READ_HOST}}}})
        raise AssertionError(f"unscripted command: {argv}")


OK = b'{"status":"ok"}'
NOT_FOUND = b'{"detail":"not found"}'
UNAVAILABLE = b'{"detail":"public API unavailable"}'
PROBE_URL = f"https://{READ_HOST}/api/v1/context"
DISABLE_URL = f"https://{HOST}/budget/disable-public-api"
CLEAR_URL = f"https://{HOST}/budget/clear"


class Clock:
    """Deterministic time: sleeping advances it, nothing else does."""

    def __init__(self):
        self.t = NOW

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class FakeCloud:
    """The Function's hooks plus the read app, with a replica cache lag.

    The durable state flips when a hook succeeds; the read app shows the new
    state ``lag`` seconds later, as a warm replica with a cached kill state does.
    """

    def __init__(self, mig, *, public=True, lag=12.0, cold=0, platform_503=0,
                 disable=((200, OK),), clear=((200, OK),), shutdown_never=False,
                 recover_never=False, baseline=None, interrupt_on=None, shutdown_platform=False):
        self.mig = mig
        self.shutdown_platform = shutdown_platform
        self.clock = Clock()
        self.calls = []  # (method, url, headers)
        self.public = public
        self.shown = public
        self.changed_at = float("-inf")
        self.lag = lag
        self.cold = cold
        self.platform_503 = platform_503
        self.disable = list(disable)
        self.clear = list(clear)
        self.shutdown_never = shutdown_never
        self.recover_never = recover_never
        self.baseline = baseline
        self.interrupt_on = interrupt_on

    def _next(self, responses):
        return responses.pop(0) if len(responses) > 1 else responses[0]

    def _set(self, public):
        self.shown = self._visible()
        self.public = public
        self.changed_at = self.clock.now()

    def _visible(self):
        if self.clock.now() - self.changed_at >= self.lag:
            return self.public
        return self.shown

    def posts(self, url):
        return [call for call in self.calls if call[0] == "POST" and call[1] == url]

    def __call__(self, method, url, headers, timeout):
        self.calls.append((method, url, dict(headers)))
        if method == "GET":
            assert url == PROBE_URL, url
            if self.interrupt_on == "probe-after-disable" and self.posts(DISABLE_URL):
                self.interrupt_on = None  # one Ctrl-C
                raise KeyboardInterrupt
            if self.baseline is not None and not self.posts(DISABLE_URL):
                return self.baseline
            if self.cold:  # scale-from-zero: no response yet
                self.cold -= 1
                raise self.mig.MigrationError("could not reach the budget hook over HTTPS")
            if self.platform_503:
                self.platform_503 -= 1
                return 503, b"upstream connect error"
            visible = self._visible()
            if self.shutdown_platform and self.posts(DISABLE_URL) and not self.posts(CLEAR_URL):
                return 503, b"upstream connect error or disconnect/reset before headers"
            if self.shutdown_never and not self.posts(CLEAR_URL):
                visible = True
            if self.recover_never and self.posts(CLEAR_URL):
                visible = False
            return (404, NOT_FOUND) if visible else (503, UNAVAILABLE)
        assert method == "POST", method
        if url == DISABLE_URL:
            status, body = self._next(self.disable)
            if status == 200:
                self._set(False)
            return status, body
        if url == CLEAR_URL:
            if self.interrupt_on == "clear":
                raise KeyboardInterrupt
            status, body = self._next(self.clear)
            if status == 200:
                self._set(True)
            return status, body
        raise AssertionError(url)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "checkout"
    params = root / "infra" / "azure" / "main.local.bicepparam"
    params.parent.mkdir(parents=True)
    params.write_text(PARAMS)
    params.chmod(0o640)
    return root


def _migrator(mig, repo, world, *, http=None, env=None, prompt=None, tty=False, **options):
    opts = mig.Options(
        resource_group=RG,
        subscription=SUB,
        function_app_name=APP,
        bootstrap_storage_name=BOOT,
        params=repo / "infra" / "azure" / "main.local.bicepparam",
        **options,
    )
    cloud = http if http is not None else FakeCloud(mig)
    return mig.Migrator(
        opts,
        runner=world,
        http=cloud,
        environ=ENV if env is None else env,
        prompt=prompt or (lambda _message: pytest.fail("unexpected prompt")),
        stdin_isatty=lambda: tty,
        which=lambda name: f"/usr/local/bin/{name}",
        sleep=cloud.clock.sleep,
        now=cloud.clock.now,
        repo_root=repo,
        python="/venv/python",
    )


def _commands(world, *words):
    return [call for call in world.calls if all(word in call for word in words)]


def _assert_no_secret(*texts):
    for text in texts:
        for secret in SECRETS:
            assert secret not in text


def _assert_argv_clean(world):
    for call in world.calls:
        _assert_no_secret(" ".join(call))


# ---------------------------------------------------------------------------
# dry-run
# ---------------------------------------------------------------------------


def test_dry_run_makes_zero_calls(mig, repo, capsys):
    def forbidden_runner(*_args, **_kwargs):
        pytest.fail("dry-run ran a subprocess")

    def forbidden_http(*_args, **_kwargs):
        pytest.fail("dry-run made a network call")

    params = repo / "infra/azure/main.local.bicepparam"
    before = params.read_bytes()
    code = mig.main(
        [
            "--resource-group", RG, "--subscription", SUB, "--function-app-name", APP,
            "--bootstrap-storage-name", BOOT, "--params", str(params), "--dry-run",
        ],
        runner=forbidden_runner,
        http=forbidden_http,
        environ=ENV,
        which=lambda name: f"/usr/local/bin/{name}",
        repo_root=repo,
        python="/venv/python",
    )
    captured = capsys.readouterr()
    assert code == 0
    assert params.read_bytes() == before
    assert not (repo / "infra/azure" / mig.BACKUP_NAME).exists()
    out = captured.out
    assert "az functionapp create" in out
    assert "--flexconsumption-location eastus2" in out
    assert "deploy_cloud.py" in out and "--always-deploy" in out
    assert "func azure functionapp publish" in out
    assert "X-Wattracker-Budget-Token: ***" in out and "x-functions-key: ***" in out
    assert "/budget/disable-public-api  headers: x-functions-key: ***\n" in out
    assert "ALWAYS: POST" in out and "/api/v1/context" in out
    assert "storage entity show" not in out
    assert "vnet create" not in out.replace("never `az network vnet create`", "")
    assert STORAGE not in out  # parameter contents are never printed
    _assert_no_secret(out, captured.err)


def test_dry_run_reports_local_findings_without_calls(mig, repo, capsys):
    world = FakeWorld(repo, mig)
    env = {k: v for k, v in ENV.items() if k != "WATTRACKER_BUDGET_HOOK_TOKEN"}
    code = _migrator(mig, repo, world, env=env, dry_run=True).execute()
    captured = capsys.readouterr()
    assert code == 1
    assert world.calls == []
    assert "WATTRACKER_BUDGET_HOOK_TOKEN must be set" in captured.err
    assert "az functionapp create" in captured.out


# ---------------------------------------------------------------------------
# the VNet and subnet
# ---------------------------------------------------------------------------


def _full_run(mig, repo, capsys=None, cloud=None, **state):
    world = FakeWorld(repo, mig, **state)
    http = cloud if cloud is not None else FakeCloud(mig)
    code = _migrator(mig, repo, world, http=http, confirm_delete=APP).execute()
    return code, world, http


@pytest.mark.parametrize("state", [
    {},
    {"app": "flex", "subnet": "ok"},
    {"subnet": "missing", "boot_storage": False, "app": "missing"},
    {"vnet": False},
    {"vnet_prefixes": ["10.99.0.0/16"]},
])
def test_vnet_create_is_never_called(mig, repo, state):
    _code, world, _http = _full_run(mig, repo, **state)
    for call in world.calls:
        joined = " ".join(call)
        assert "vnet create" not in joined
        assert not (call[:2] == ["az", "rest"] and "/subnets/budget-hook-flex?" not in joined)


def test_missing_vnet_stops_with_a_message_and_creates_nothing(mig, repo, capsys):
    code, world, _http = _full_run(mig, repo, vnet=False)
    err = capsys.readouterr().err
    assert code == 1
    assert "wattracker-vnet does not exist" in err
    assert "never creates" in err
    assert not any("create" in call or "put" in call for call in world.calls)


def test_vnet_address_space_must_contain_the_flex_subnet(mig, repo, capsys):
    code, world, _http = _full_run(mig, repo, vnet_prefixes=["10.42.0.0/23"])
    assert code == 1
    assert "does not contain 10.42.2.0/27" in capsys.readouterr().err
    assert not _commands(world, "subnet", "show")


def test_missing_subnet_is_created_with_the_main_bicep_body(mig, repo):
    code, world, _http = _full_run(mig, repo, subnet="missing")
    assert code == 0
    (put,) = [call for call in world.calls if call[:2] == ["az", "rest"]]
    body = json.loads(put[put.index("--body") + 1])
    assert body == {"properties": {
        "addressPrefix": "10.42.2.0/27",
        "delegations": [{"name": "budget-hook-flex",
                         "properties": {"serviceName": "Microsoft.App/environments"}}],
        "serviceEndpoints": [{"service": "Microsoft.Storage", "locations": ["eastus2"]}],
    }}
    # The body must equal what main.bicep declares, so step 2 is a no-op on it.
    bicep = (ROOT / "infra/azure/main.bicep").read_text()
    block = bicep.split("resource budgetHookSubnet ", 1)[1].split("\nresource ", 1)[0]
    assert "addressPrefix: '10.42.2.0/27'" in block
    assert "name: 'budget-hook-flex'" in block.split("delegations", 1)[1]
    assert "serviceName: 'Microsoft.App/environments'" in block
    assert "service: 'Microsoft.Storage'" in block and "locations: [location]" in block


def test_existing_subnet_storage_and_flex_app_are_not_recreated(mig, repo):
    code, world, _http = _full_run(mig, repo, app="flex")
    assert code == 0
    assert not [call for call in world.calls if call[:2] == ["az", "rest"]]
    assert not _commands(world, "storage", "account", "create")
    assert not _commands(world, "functionapp", "delete")
    assert not _commands(world, "functionapp", "create")
    assert not _commands(world, "identity", "assign")


def test_missing_bootstrap_storage_is_created_standard_lrs(mig, repo):
    code, world, _http = _full_run(mig, repo, boot_storage=False)
    assert code == 0
    (create,) = _commands(world, "storage", "account", "create")
    assert create[create.index("--sku") + 1] == "Standard_LRS"
    assert create[create.index("--name") + 1] == BOOT


@pytest.mark.parametrize("override", [
    {"addressPrefix": "10.42.3.0/27"},
    {"delegations": [{"name": "d", "serviceName": "Microsoft.Web/serverFarms"}]},
    {"delegations": []},
    {"serviceEndpoints": [{"service": "Microsoft.KeyVault"}]},
])
def test_subnet_property_mismatch_stops_before_any_write(mig, repo, capsys, override):
    base = FakeWorld(repo, mig)._subnet()
    code, world, _http = _full_run(mig, repo, subnet={**base, **override})
    err = capsys.readouterr().err
    assert code == 1
    assert "budget-hook-flex exists but" in err
    assert "--from-step 1" in err
    assert not _commands(world, "functionapp", "delete")
    assert not _commands(world, "functionapp", "create")
    assert not _commands(world, "storage", "account", "create")


# ---------------------------------------------------------------------------
# the one destructive step
# ---------------------------------------------------------------------------


def test_delete_runs_after_a_matching_typed_confirmation(mig, repo):
    world = FakeWorld(repo, mig, app="y1")
    prompts = []
    migrator = _migrator(
        mig, repo, world, tty=True, prompt=lambda message: prompts.append(message) or APP
    )
    assert migrator.execute() == 0
    assert len(prompts) == 1 and APP in prompts[0]
    delete_index = world.calls.index(_commands(world, "functionapp", "delete")[0])
    create_index = world.calls.index(_commands(world, "functionapp", "create")[0])
    assert delete_index < create_index


def test_wrong_typed_name_aborts_without_deleting(mig, repo, capsys):
    world = FakeWorld(repo, mig, app="y1")
    migrator = _migrator(mig, repo, world, tty=True, prompt=lambda _m: APP + "-typo")
    assert migrator.execute() == 1
    assert "nothing was deleted" in capsys.readouterr().err
    assert not _commands(world, "functionapp", "delete")
    assert not _commands(world, "functionapp", "create")


def test_wrong_confirm_delete_flag_aborts_before_any_call(mig, repo, capsys):
    world = FakeWorld(repo, mig, app="y1")
    assert _migrator(mig, repo, world, confirm_delete="other-app").execute() == 1
    assert "nothing was deleted" in capsys.readouterr().err
    assert world.calls == []


def test_non_interactive_without_confirmation_refuses_to_delete(mig, repo, capsys):
    world = FakeWorld(repo, mig, app="y1")
    assert _migrator(mig, repo, world, tty=False).execute() == 1
    assert f"--confirm-delete {APP}" in capsys.readouterr().err
    assert not _commands(world, "functionapp", "delete")


def test_matching_confirm_delete_flag_deletes_then_creates_flex(mig, repo):
    code, world, _http = _full_run(mig, repo, app="y1")
    assert code == 0
    assert len(_commands(world, "functionapp", "delete")) == 1
    (create,) = _commands(world, "functionapp", "create")
    assert create[create.index("--flexconsumption-location") + 1] == "eastus2"
    assert create[create.index("--runtime") + 1] == "python"
    assert create[create.index("--runtime-version") + 1] == "3.12"
    assert create[create.index("--functions-version") + 1] == "4"
    assert create[create.index("--vnet") + 1] == SUBNET_ID.rsplit("/subnets/", 1)[0]
    assert create[create.index("--subnet") + 1] == "budget-hook-flex"
    assert create[create.index("--storage-account") + 1] == BOOT
    assert _commands(world, "identity", "assign")


def test_flex_app_that_is_not_integrated_stops(mig, repo, capsys):
    code, world, _http = _full_run(mig, repo, app="flex-unintegrated")
    assert code == 1
    assert "not integrated with budget-hook-flex" in capsys.readouterr().err
    assert not _commands(world, "functionapp", "delete")


# ---------------------------------------------------------------------------
# the parameter file
# ---------------------------------------------------------------------------


def test_params_edit_changes_exactly_three_values_and_keeps_a_0600_backup(mig, repo):
    params = repo / "infra/azure/main.local.bicepparam"
    original = params.read_bytes()
    code, _world, _http = _full_run(mig, repo)
    assert code == 0
    updated = params.read_bytes()
    before, after = original.decode().splitlines(True), updated.decode().splitlines(True)
    assert len(before) == len(after)
    changed = [(a, b) for a, b in zip(before, after) if a != b]
    assert len(changed) == 3
    assert f"param budgetHookPrincipalId = '{PID}'  // old Y1 identity\n" in after
    assert f"param budgetHookHost = '{HOST}'\n" in after
    assert f"param budgetHookFunctionAppName = '{APP}'\n" in after
    assert "https://" not in updated.decode()
    assert stat.S_IMODE(params.stat().st_mode) == 0o640
    backup = params.parent / mig.BACKUP_NAME
    assert backup.read_bytes() == original
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_params_rerun_is_a_no_op_and_keeps_the_first_backup(mig, repo):
    params = repo / "infra/azure/main.local.bicepparam"
    original = params.read_bytes()
    assert _full_run(mig, repo)[0] == 0
    once = params.read_bytes()
    world = FakeWorld(repo, mig, app="flex", deployed=True)
    shutil.rmtree(repo / "build")
    assert _migrator(mig, repo, world, from_step=2).execute() == 0
    assert params.read_bytes() == once
    assert (params.parent / mig.BACKUP_NAME).read_bytes() == original


def test_tracked_params_file_is_refused_before_azure(mig, repo, capsys):
    code, world, _http = _full_run(mig, repo, tracked=True)
    assert code == 1
    assert "refusing a tracked parameter file" in capsys.readouterr().err
    assert all(call[0] == "git" for call in world.calls)


@pytest.mark.parametrize("mutate, message", [
    (lambda text: text.replace("param budgetHookHost = 'old-y1-host.azurewebsites.net'\n", ""),
     "does not declare budgetHookHost"),
    (lambda text: text + "param budgetHookPrincipalId = 'dup'\n",
     "declares budgetHookPrincipalId more than once"),
    (lambda text: text + "param budgetHookIpRules = [\n  '1.2.3.4'\n]\n",
     "still declares budgetHookIpRules"),
    (lambda text: text.replace("param storageName", "// param storageName"),
     "does not declare storageName"),
])
def test_params_structure_problems_stop_before_any_call(mig, repo, capsys, mutate, message):
    params = repo / "infra/azure/main.local.bicepparam"
    params.write_text(mutate(PARAMS))
    before = params.read_bytes()
    code, world, _http = _full_run(mig, repo)
    err = capsys.readouterr().err
    assert code == 1
    assert message in err
    assert world.calls == []
    assert params.read_bytes() == before
    assert "owner@example.invalid" not in err


def test_params_must_be_the_deployment_file_inside_the_checkout(mig, repo, tmp_path, capsys):
    other = repo / "infra/azure/main.copy.bicepparam"
    other.write_text(PARAMS)
    world = FakeWorld(repo, mig)
    migrator = _migrator(mig, repo, world, confirm_delete=APP)
    migrator.o.params = other
    assert migrator.execute() == 1
    assert "infra/azure/main.local.bicepparam" in capsys.readouterr().err
    outside = tmp_path / "outside.bicepparam"
    outside.write_text(PARAMS)
    migrator = _migrator(mig, repo, world)
    migrator.o.params = outside
    assert migrator.execute() == 1
    assert "inside this checkout" in capsys.readouterr().err
    assert world.calls == []


def test_unset_environment_read_by_params_stops_before_the_destructive_step(mig, repo, capsys):
    env = {k: v for k, v in ENV.items() if k != "WATTRACKER_OPERATOR_TOKEN"}
    world = FakeWorld(repo, mig)
    assert _migrator(mig, repo, world, env=env, confirm_delete=APP).execute() == 1
    err = capsys.readouterr().err
    assert "WATTRACKER_OPERATOR_TOKEN" in err
    assert "operator-token-value" not in err
    assert world.calls == []


# ---------------------------------------------------------------------------
# step 2 verification
# ---------------------------------------------------------------------------


def test_deploy_cloud_runs_with_inherited_stdio_and_always_deploy(mig, repo):
    code, world, _http = _full_run(mig, repo)
    assert code == 0
    (deploy,) = [call for call in world.calls if call[1].endswith("deploy_cloud.py")]
    assert deploy[2] == str(repo / "infra/azure/main.local.bicepparam")
    assert deploy[3:] == ["--resource-group", RG, "--always-deploy"]


def test_deploy_that_did_not_reach_azure_fails_step_two(mig, repo, capsys):
    code, world, _http = _full_run(mig, repo, deploy_reaches_azure=False)
    err = capsys.readouterr().err
    assert code == 1
    assert "no virtualNetworkRules entry for budget-hook-flex" in err
    assert "--from-step 2" in err
    assert not _commands(world, "appsettings", "set")


def test_missing_role_assignment_for_the_new_principal_fails_step_two(mig, repo, capsys):
    code, world, _http = _full_run(mig, repo, role_assigned=False)
    err = capsys.readouterr().err
    assert code == 1
    assert "no Wattracker Budget Hook Writer assignment" in err
    assert "--from-step 2" in err
    assert not _commands(world, "appsettings", "set")


@pytest.mark.parametrize("state, message", [
    ({"default_action": "Allow"}, "defaultAction is not Deny"),
    ({"ip_rules": [{"ipAddressOrRange": "20.1.2.3"}]}, "still has IP rules"),
])
def test_storage_firewall_must_be_deny_with_no_ip_rules(mig, repo, capsys, state, message):
    code, _world, _http = _full_run(mig, repo, **state)
    assert code == 1
    assert message in capsys.readouterr().err


def test_deploy_cloud_failure_names_the_resume_step(mig, repo, capsys):
    code, world, _http = _full_run(mig, repo, deploy_rc=1)
    err = capsys.readouterr().err
    assert code == 1
    assert "step 2 (parameters and deploy) failed" in err
    assert "--from-step 2" in err
    assert f"--confirm-delete {APP}" in err
    assert not _commands(world, "appsettings", "set")


# ---------------------------------------------------------------------------
# secrets
# ---------------------------------------------------------------------------


def test_secrets_never_reach_argv_output_or_the_summary(mig, repo, capsys):
    code, world, http = _full_run(mig, repo)
    captured = capsys.readouterr()
    assert code == 0
    _assert_argv_clean(world)
    _assert_no_secret(captured.out, captured.err)
    # The secrets did travel where they must: hook headers and the settings file.
    assert http.posts(DISABLE_URL) and http.posts(CLEAR_URL)
    (settings,) = world.settings_files
    assert {"name": "WATTRACKER_BUDGET_HOOK_TOKEN", "value": TOKEN, "slotSetting": False} in settings["content"]
    assert {"name": "WATTRACKER_STORAGE_ACCOUNT_NAME", "value": STORAGE, "slotSetting": False} in settings["content"]
    assert settings["mode"] == 0o600
    assert "----- paste into #339 -----" in captured.out
    assert PID in captured.out and HOST in captured.out and SUBNET_ID in captured.out


@pytest.mark.parametrize("state", [
    {"fail": (["az", "functionapp", "keys", "list"],)},
    {"fail": (["az", "functionapp", "config", "appsettings", "set"],)},
    {"fail": (["func"],)},
])
def test_secrets_stay_out_of_failure_output(mig, repo, capsys, state):
    code, world, _http = _full_run(mig, repo, **state)
    captured = capsys.readouterr()
    assert code == 1
    _assert_argv_clean(world)
    _assert_no_secret(captured.out, captured.err)


_LEAKY = f"bad {HOST_KEY} {TOKEN} {MASTER_KEY}".encode()


@pytest.mark.parametrize("cloud_kwargs", [
    {"disable": ((401, _LEAKY),)},
    {"disable": ((200, _LEAKY),)},
    {"shutdown_never": True},
    {"recover_never": True},
    {"clear": ((500, _LEAKY),)},
    {"interrupt_on": "clear"},
    {"interrupt_on": "probe-after-disable"},
    {"baseline": (503, UNAVAILABLE)},
    {"baseline": (200, _LEAKY)},
])
def test_secrets_stay_out_of_drill_failure_output(mig, repo, capsys, cloud_kwargs):
    code, world, _cloud = _full_run(mig, repo, cloud=FakeCloud(mig, **cloud_kwargs))
    captured = capsys.readouterr()
    assert code != 0
    _assert_argv_clean(world)
    _assert_no_secret(captured.out, captured.err)


def test_secrets_stay_out_of_exception_text(mig, repo):
    world = FakeWorld(repo, mig, app="flex", deployed=True)
    cloud = FakeCloud(mig, disable=((401, _LEAKY),), recover_never=True)
    migrator = _migrator(mig, repo, world, http=cloud)
    migrator.local_preflight()
    migrator.read_app_facts()
    with pytest.raises(mig.MigrationError) as drill:
        migrator.step4_drill()
    world.fail = (["az", "functionapp", "config", "appsettings", "set"],)
    with pytest.raises(mig.MigrationError) as settings:
        migrator.set_app_settings(STORAGE)
    world.fail = (["az", "functionapp", "keys", "list"],)
    with pytest.raises(mig.MigrationError) as keys:
        migrator.host_key()
    world.fail = ()
    migrator.http = FakeCloud(mig, clear=((500, _LEAKY),))
    migrator.drill = mig.DrillRecord()
    with pytest.raises(mig.MigrationError) as clear:
        migrator.hook_post("/budget/clear", {"x-functions-key": HOST_KEY})
    for info in (drill, settings, keys, clear):
        text = str(info.value) + repr(info.value) + repr(info.value.__cause__) + repr(info.value.__context__)
        _assert_no_secret(text)


def test_default_runner_strips_the_token_from_child_environments(mig, monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return _result(argv, 0)

    monkeypatch.setenv("WATTRACKER_BUDGET_HOOK_TOKEN", TOKEN)
    monkeypatch.setattr(mig.subprocess, "run", fake_run)
    mig.default_runner(["az", "version"])
    assert "WATTRACKER_BUDGET_HOOK_TOKEN" not in seen["env"]
    assert TOKEN not in json.dumps(seen["env"])


def test_default_http_refuses_plain_http_and_never_follows_redirects(mig):
    for method in ("GET", "POST"):
        with pytest.raises(mig.MigrationError, match="non-HTTPS"):
            mig.default_http(method, "http://example.invalid/budget/clear", {}, 1.0)
    assert mig._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil") is None


def test_token_is_never_accepted_on_the_command_line(mig):
    parser = mig.build_parser()
    options = {action.dest for action in parser._actions}
    assert not any("token" in option for option in options)


def test_settings_temp_file_is_removed_even_on_failure(mig, repo):
    code, world, _http = _full_run(mig, repo, settings_rc=1)
    assert code == 1
    (settings,) = world.settings_files
    assert settings["mode"] == 0o600
    assert not settings["path"].exists()
    assert not str(settings["path"]).startswith(str(repo))


def test_settings_temp_file_is_removed_on_success(mig, repo):
    code, world, _http = _full_run(mig, repo)
    assert code == 0
    (settings,) = world.settings_files
    assert not settings["path"].exists()


# ---------------------------------------------------------------------------
# step 3 staging
# ---------------------------------------------------------------------------


def test_existing_staged_dir_stops_before_the_destructive_step(mig, repo, capsys):
    (repo / "build/azure-budget-hook").mkdir(parents=True)
    code, world, _http = _full_run(mig, repo)
    err = capsys.readouterr().err
    assert code == 1
    assert "rm -rf -- build/azure-budget-hook" in err
    assert world.calls == []
    assert (repo / "build/azure-budget-hook").is_dir()  # never removed automatically


def test_publish_runs_from_the_staged_dir_after_settings(mig, repo):
    code, world, _http = _full_run(mig, repo)
    assert code == 0
    names = [" ".join(call[:5]) for call in world.calls]
    settings = next(i for i, n in enumerate(names) if "appsettings set" in n)
    package = next(i for i, call in enumerate(world.calls) if call[1:2] and call[1].endswith("package_budget_hook.py"))
    publish = next(i for i, call in enumerate(world.calls) if call[0] == "func")
    assert settings < package < publish
    assert world.calls[publish] == ["func", "azure", "functionapp", "publish", APP, "--python"]
    assert world.cwds[publish] == repo / "build/azure-budget-hook"


# ---------------------------------------------------------------------------
# the drill: disable -> observe 503 -> ALWAYS clear -> observe recovery
# ---------------------------------------------------------------------------


def _drill(mig, repo, capsys, **cloud_kwargs):
    cloud = FakeCloud(mig, **cloud_kwargs)
    code, world, _ = _full_run(mig, repo, cloud=cloud)
    captured = capsys.readouterr()
    return code, cloud, world, captured


def _order(cloud):
    return [(method, url) for method, url, _headers in cloud.calls if method == "POST"]


def test_drill_full_pass(mig, repo, capsys):
    code, cloud, _world, captured = _drill(mig, repo, capsys)
    assert code == 0
    assert _order(cloud) == [("POST", DISABLE_URL), ("POST", CLEAR_URL)]
    out = captured.out
    assert "PASS: disable shut the public API, clear restored it" in out
    assert "  - baseline: HTTP 404" in out
    assert "  - `POST /budget/disable-public-api`: HTTP 200" in out
    assert "  - disable -> 503: 12s" in out
    assert "  - `POST /budget/clear`: HTTP 200" in out
    assert "  - clear -> 404 recovery: 12s" in out
    assert "  - drill result: PASS" in out
    for step in range(5):
        assert f"| {step}. " in out
    assert "| FAIL |" not in out


def test_probe_is_anonymous_and_https_on_the_read_app(mig, repo, capsys):
    _code, cloud, world, _captured = _drill(mig, repo, capsys)
    probes = [(url, headers) for method, url, headers in cloud.calls if method == "GET"]
    assert probes and all(url == PROBE_URL for url, _ in probes)
    for _url, headers in probes:
        assert headers == {"Accept": "application/json"}
    (show,) = _commands(world, "containerapp", "show")
    assert show[show.index("--name") + 1] == "wattracker-read"


def test_app_token_goes_to_clear_but_never_to_disable(mig, repo, capsys):
    _code, cloud, _world, _captured = _drill(mig, repo, capsys)
    ((_, _, disable_headers),) = [c for c in cloud.calls if c[1] == DISABLE_URL]
    ((_, _, clear_headers),) = [c for c in cloud.calls if c[1] == CLEAR_URL]
    assert disable_headers == {"x-functions-key": HOST_KEY}
    assert clear_headers == {"x-functions-key": HOST_KEY, "X-Wattracker-Budget-Token": TOKEN}


def test_disable_public_api_needs_no_app_token_in_the_functions_shape():
    """The reason the drill may omit the token: platform auth on this route."""

    hook = (ROOT / "wattracker/cloud/budget_hook.py").read_text()
    entry = (ROOT / "infra/azure/budget-hook/function_app.py").read_text()
    assert "platform_authenticated=True" in entry
    disable = hook.split('@app.post("/budget/disable-public-api")', 1)[1].split("@app.post", 1)[0]
    assert "await apply(" in disable
    clear = hook.split('@app.post("/budget/clear")', 1)[1]
    assert "authenticate_header(request)" in clear


@pytest.mark.parametrize("baseline", [
    (503, UNAVAILABLE),
])
def test_baseline_503_stops_before_anything_is_disabled(mig, repo, capsys, baseline):
    code, cloud, _world, captured = _drill(mig, repo, capsys, baseline=baseline)
    assert code == 1
    assert _order(cloud) == []
    assert "kill switch is on or its state is unreadable" in captured.err
    assert "Nothing was disabled" in captured.err
    assert "/budget/clear" in captured.err and "--from-step 4" in captured.err


def test_baseline_rides_out_a_cold_start_and_platform_503s(mig, repo, capsys):
    code, cloud, _world, _captured = _drill(mig, repo, capsys, cold=4, platform_503=2)
    assert code == 0
    assert _order(cloud) == [("POST", DISABLE_URL), ("POST", CLEAR_URL)]


def test_unexpected_baseline_stops_before_anything_is_disabled(mig, repo, capsys):
    code, cloud, _world, captured = _drill(mig, repo, capsys, baseline=(200, b"{}"))
    assert code == 1
    assert _order(cloud) == []
    assert "not the neutral 404" in captured.err


def test_no_503_within_the_timeout_fails_and_still_clears(mig, repo, capsys):
    code, cloud, _world, captured = _drill(mig, repo, capsys, shutdown_never=True)
    assert code == 1
    assert _order(cloud) == [("POST", DISABLE_URL), ("POST", CLEAR_URL)]
    assert "did not answer HTTP 503 within 90s" in captured.err
    assert "  - disable -> 503: not observed" in captured.out
    assert "  - drill result: FAIL" in captured.out


def test_a_platform_503_is_not_proof_of_the_shutdown(mig, repo, capsys):
    """Only the app's own 'public API unavailable' 503 proves the kill switch."""

    code, cloud, _world, captured = _drill(mig, repo, capsys, shutdown_platform=True)
    assert code == 1
    assert _order(cloud) == [("POST", DISABLE_URL), ("POST", CLEAR_URL)]
    assert "did not answer HTTP 503" in captured.err
    assert "(last: HTTP 503)" in captured.err


def test_shutdown_timeout_is_the_kill_switch_ttl_plus_a_cold_start_margin(mig, repo, capsys):
    from wattracker.cloud.limits import KILL_SWITCH_TTL_SECONDS

    assert mig.OBSERVE_TIMEOUT_SECONDS == KILL_SWITCH_TTL_SECONDS + 60.0
    cloud = FakeCloud(mig, shutdown_never=True)
    _full_run(mig, repo, cloud=cloud)
    capsys.readouterr()
    disabled_at = next(i for i, c in enumerate(cloud.calls) if c[1] == DISABLE_URL)
    polls = [c for c in cloud.calls[disabled_at:] if c[0] == "GET"]
    polls_until_clear = []
    for call in cloud.calls[disabled_at + 1:]:
        if call[1] == CLEAR_URL:
            break
        polls_until_clear.append(call)
    assert polls
    assert len(polls_until_clear) == int(mig.OBSERVE_TIMEOUT_SECONDS / mig.POLL_INTERVAL_SECONDS) + 1


@pytest.mark.parametrize("cloud_kwargs, message", [
    ({"disable": ((401, b'{"detail":"unauthorized"}'),)}, "returned HTTP 401"),
    ({"disable": ((200, b'{"status": "ok"}'),)}, 'not exactly {"status":"ok"}'),
    ({"disable": ((0, b""),)}, "returned HTTP no response"),
    ({"disable": ((302, b""),)}, "returned HTTP 302"),
    ({"shutdown_never": True}, "did not answer HTTP 503"),
])
def test_clear_runs_after_every_failure_once_disable_was_attempted(
    mig, repo, capsys, cloud_kwargs, message
):
    code, cloud, _world, captured = _drill(mig, repo, capsys, **cloud_kwargs)
    assert code == 1
    posts = _order(cloud)
    assert ("POST", DISABLE_URL) in posts
    assert posts[-1] == ("POST", CLEAR_URL)
    assert message in captured.err
    assert "step 4 (drill) failed" in captured.err
    assert "  - `POST /budget/clear`: HTTP 200" in captured.out
    assert "  - drill result: FAIL" in captured.out
    if "disable" in cloud_kwargs:
        status = cloud_kwargs["disable"][0][0]
        assert f"  - `POST /budget/disable-public-api`: HTTP {status or 'no response'}" in captured.out


def test_ctrl_c_after_disable_still_clears(mig, repo, capsys):
    code, cloud, _world, captured = _drill(mig, repo, capsys, interrupt_on="probe-after-disable")
    assert code == 130
    assert _order(cloud) == [("POST", DISABLE_URL), ("POST", CLEAR_URL)]
    # The interrupt is honoured after the clear: no recovery polling follows.
    assert cloud.calls[-1][1] == CLEAR_URL
    assert "interrupted" in captured.err
    assert "LEFT DISABLED" not in captured.err
    assert "  - `POST /budget/clear`: HTTP 200" in captured.out


@pytest.mark.parametrize("cloud_kwargs", [
    {"clear": ((500, b'{"detail":"budget hook unavailable"}'),)},
    {"clear": ((503, b""),)},
    {"interrupt_on": "clear"},
])
def test_clear_failure_warns_loudly_and_exits_nonzero(mig, repo, capsys, cloud_kwargs):
    code, cloud, _world, captured = _drill(mig, repo, capsys, **cloud_kwargs)
    assert code != 0
    err = captured.err
    assert "THE CLOUD PUBLIC API IS LEFT DISABLED" in err
    assert "curl --fail-with-body --request POST" in err
    assert f"https://{HOST}/budget/clear" in err
    assert '"x-functions-key: $FUNCTION_HOST_KEY"' in err
    assert '"X-Wattracker-Budget-Token: $WATTRACKER_BUDGET_HOOK_TOKEN"' in err
    assert "--query functionKeys.default --output tsv" in err
    assert "**the public API was left DISABLED: clear failed**" in captured.out
    if "clear" in cloud_kwargs:
        status = cloud_kwargs["clear"][0][0]
        assert f"  - `POST /budget/clear`: HTTP {status}" in captured.out
    _assert_no_secret(captured.out, err)
    # No recovery poll is attempted against a cloud that was never cleared.
    last_clear = max(i for i, c in enumerate(cloud.calls) if c[1] == CLEAR_URL)
    assert not [c for c in cloud.calls[last_clear + 1:] if c[0] == "GET"]


def test_clear_retries_a_cold_start(mig, repo, capsys):
    code, cloud, _world, _captured = _drill(mig, repo, capsys, clear=((503, b""), (200, OK)))
    assert code == 0
    assert len(cloud.posts(CLEAR_URL)) == 2


def test_no_recovery_within_the_timeout_fails(mig, repo, capsys):
    code, cloud, _world, captured = _drill(mig, repo, capsys, recover_never=True)
    assert code == 1
    assert _order(cloud) == [("POST", DISABLE_URL), ("POST", CLEAR_URL)]
    assert "FAIL: recovery" in captured.err
    assert "  - clear -> 404 recovery: not observed" in captured.out
    assert "  - drill result: FAIL" in captured.out


def test_shutdown_failure_and_no_recovery_are_both_reported(mig, repo, capsys):
    code, _cloud, _world, captured = _drill(
        mig, repo, capsys, disable=((401, b""),), recover_never=True
    )
    assert code == 1
    assert "returned HTTP 401" in captured.err and "FAIL: recovery" in captured.err


def test_drill_disable_retries_a_cold_start_then_passes(mig, repo, capsys):
    code, cloud, _world, _captured = _drill(mig, repo, capsys, disable=((503, b""), (200, OK)))
    assert code == 0
    assert len(cloud.posts(DISABLE_URL)) == 2


def test_missing_read_app_stops_before_anything_is_disabled(mig, repo, capsys):
    cloud = FakeCloud(mig)
    code, _world, _ = _full_run(mig, repo, cloud=cloud, read_app=False)
    assert code == 1
    assert "wattracker-read does not exist" in capsys.readouterr().err
    assert cloud.calls == []


def test_probe_contract_against_the_real_read_app():
    """GET /api/v1/context, anonymous: neutral 404 serving, 503 when disabled."""

    from fastapi.testclient import TestClient

    from wattracker.cloud.api import _NOT_FOUND_BODY, CloudConfig, CloudState, create_cloud_app
    from wattracker.cloud.limits import PUBLIC_UNAVAILABLE_DETAIL

    mig = _load()
    assert _NOT_FOUND_BODY == {"detail": mig.NOT_FOUND_DETAIL}
    assert mig.PUBLIC_UNAVAILABLE_DETAIL == PUBLIC_UNAVAILABLE_DETAIL
    for gateway in (False, True):
        config = CloudConfig(
            server_secret=b"cloud-test-server-secret-32-bytes-long",
            operator_token="operator-token",
            plane="read",
            require_gateway_proof=gateway,
            gateway_proof_value="gateway-proof" if gateway else "",
            clock=lambda: 1_000,
        )
        state = CloudState.create(config)
        with TestClient(create_cloud_app(config, state=state)) as client:
            serving = client.get(mig.PROBE_PATH, headers={"Accept": "application/json"})
            state.quotas.set_public_enabled(False)
            disabled = client.get(mig.PROBE_PATH, headers={"Accept": "application/json"})
            state.quotas.set_public_enabled(True)
            recovered = client.get(mig.PROBE_PATH, headers={"Accept": "application/json"})
        assert (serving.status_code, serving.json()) == (404, {"detail": mig.NOT_FOUND_DETAIL})
        assert (disabled.status_code, disabled.json()) == (503, {"detail": PUBLIC_UNAVAILABLE_DETAIL})
        assert (recovered.status_code, recovered.json()) == (404, {"detail": mig.NOT_FOUND_DETAIL})
    assert mig.PROBE_PATH != "/api/v1/admin/version"


# ---------------------------------------------------------------------------
# resuming
# ---------------------------------------------------------------------------


def test_from_step_three_skips_steps_one_and_two(mig, repo, capsys):
    world = FakeWorld(repo, mig, app="flex", deployed=True)
    params = repo / "infra/azure/main.local.bicepparam"
    before = params.read_bytes()
    assert _migrator(mig, repo, world, from_step=3).execute() == 0
    out = capsys.readouterr().out
    assert not _commands(world, "subnet", "show")
    assert not [call for call in world.calls if call[:2] == ["az", "rest"]]
    assert not _commands(world, "functionapp", "create")
    assert not _commands(world, "functionapp", "delete")
    assert not [call for call in world.calls if call[1].endswith("deploy_cloud.py")]
    assert params.read_bytes() == before
    assert _commands(world, "appsettings", "set")
    assert "| 1. Flex Function | skipped (--from-step) |" in out
    assert "| 2. parameters and deploy | skipped (--from-step) |" in out


def test_from_step_four_runs_only_preflight_and_the_drill(mig, repo):
    world = FakeWorld(repo, mig, app="flex", deployed=True)
    (repo / "build/azure-budget-hook").mkdir(parents=True)  # left by a prior publish
    cloud = FakeCloud(mig)
    assert _migrator(mig, repo, world, http=cloud, from_step=4).execute() == 0
    assert not _commands(world, "appsettings", "set")
    assert not [call for call in world.calls if call[0] == "func"]
    assert cloud.posts(DISABLE_URL) and cloud.posts(CLEAR_URL)


def test_resuming_step_one_after_a_delete_creates_without_deleting_again(mig, repo):
    code, world, _http = _full_run(mig, repo, app="missing", identity=False)
    assert code == 0
    assert not _commands(world, "functionapp", "delete")
    assert len(_commands(world, "functionapp", "create")) == 1


def test_resume_step_two_without_a_flex_app_points_back_to_step_one(mig, repo, capsys):
    world = FakeWorld(repo, mig, app="y1")
    assert _migrator(mig, repo, world, from_step=2).execute() == 1
    err = capsys.readouterr().err
    assert "not Flex Consumption; resume with --from-step 1" in err
    assert not [call for call in world.calls if call[1].endswith("deploy_cloud.py")]


def test_unexpected_exception_is_reported_by_type_with_the_resume_step(mig, repo, capsys):
    world = FakeWorld(repo, mig)
    migrator = _migrator(mig, repo, world, confirm_delete=APP)

    def boom():
        raise ValueError(f"leak {TOKEN}")

    migrator.step3_publish = boom
    assert migrator.execute() == 1
    err = capsys.readouterr().err
    assert "unexpected ValueError" in err and "--from-step 3" in err
    _assert_no_secret(err)
