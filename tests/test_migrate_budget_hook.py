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


def _row(*, writes=True, public=True, reason="operator clear", updated_at=NOW + 1, mig=None):
    return {
        "PartitionKey": "__wattracker_auth_v1__",
        "RowKey": mig.KILL_SWITCH_ROW_KEY,
        "Payload": json.dumps({
            "writes_enabled": writes,
            "public_enabled": public,
            "reason": reason,
            "updated_at": updated_at,
        }, sort_keys=True, separators=(",", ":")),
    }


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
        self.row = state.get("row", "fresh")  # fresh | missing | forbidden | dict
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
        if argv[1:4] == ["storage", "entity", "show"]:
            if self.row == "missing":
                return _result(argv, 1, "", "The specified resource does not exist. ResourceNotFound")
            if self.row == "forbidden":
                return _result(argv, 1, "", "AuthorizationFailure: This request is not authorized")
            row = _row(mig=self.mig) if self.row == "fresh" else self.row
            return self._json(argv, row)
        raise AssertionError(f"unscripted command: {argv}")


class FakeHttp:
    def __init__(self, responses=None):
        self.responses = list(responses or [(200, b'{"status":"ok"}')])
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers)))
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "checkout"
    params = root / "infra" / "azure" / "main.local.bicepparam"
    params.parent.mkdir(parents=True)
    params.write_text(PARAMS)
    params.chmod(0o640)
    return root


def _migrator(mig, repo, world, *, http=None, env=None, prompt=None, tty=False, clock=None, **options):
    opts = mig.Options(
        resource_group=RG,
        subscription=SUB,
        function_app_name=APP,
        bootstrap_storage_name=BOOT,
        params=repo / "infra" / "azure" / "main.local.bicepparam",
        **options,
    )
    ticks = iter(range(10_000))
    return mig.Migrator(
        opts,
        runner=world,
        http_post=http or FakeHttp(),
        environ=ENV if env is None else env,
        prompt=prompt or (lambda _message: pytest.fail("unexpected prompt")),
        stdin_isatty=lambda: tty,
        which=lambda name: f"/usr/local/bin/{name}",
        sleep=lambda _seconds: None,
        now=clock or (lambda: NOW + next(ticks) * 0.001),
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
        http_post=forbidden_http,
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


def _full_run(mig, repo, capsys=None, **state):
    world = FakeWorld(repo, mig, **state)
    http = FakeHttp()
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
    # The secrets did travel where they must: headers and the settings file.
    (url, headers), = http.calls
    assert url == f"https://{HOST}/budget/clear"
    assert headers == {"x-functions-key": HOST_KEY, "X-Wattracker-Budget-Token": TOKEN}
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
    {"row": "missing"},
    {"row": "forbidden"},
])
def test_secrets_stay_out_of_failure_output(mig, repo, capsys, state):
    code, world, _http = _full_run(mig, repo, **state)
    captured = capsys.readouterr()
    assert code == 1
    _assert_argv_clean(world)
    _assert_no_secret(captured.out, captured.err)


def test_secrets_stay_out_of_exception_text(mig, repo):
    world = FakeWorld(repo, mig, app="flex", deployed=True)
    migrator = _migrator(mig, repo, world, http=FakeHttp([(401, f"bad {HOST_KEY} {TOKEN}".encode())]))
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

    def exploding_http(*_args):
        raise mig.MigrationError("could not reach the budget hook over HTTPS")

    migrator.http_post = exploding_http
    world.fail = ()
    with pytest.raises(mig.MigrationError) as network:
        migrator.post_clear()
    for info in (drill, settings, keys, network):
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


def test_default_http_post_refuses_plain_http_and_never_follows_redirects(mig):
    with pytest.raises(mig.MigrationError, match="non-HTTPS"):
        mig.default_http_post("http://example.invalid/budget/clear", {}, 1.0)
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
# the drill
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("response, message", [
    ((200, b'{"status": "ok"}'), 'not exactly {"status":"ok"}'),
    ((200, b""), 'not exactly {"status":"ok"}'),
    ((401, b'{"detail":"unauthorized"}'), "returned HTTP 401"),
])
def test_drill_wrong_response_fails(mig, repo, capsys, response, message):
    world = FakeWorld(repo, mig)
    code = _migrator(mig, repo, world, http=FakeHttp([response]), confirm_delete=APP).execute()
    assert code == 1
    err = capsys.readouterr().err
    assert message in err and "--from-step 4" in err
    assert not _commands(world, "storage", "entity", "show")


def test_drill_200_without_the_row_fails(mig, repo, capsys):
    code, _world, _http = _full_run(mig, repo, row="missing")
    assert code == 1
    assert "row does not exist" in capsys.readouterr().err


def test_drill_unreadable_row_is_not_a_pass(mig, repo, capsys):
    code, _world, _http = _full_run(mig, repo, row="forbidden")
    assert code == 1
    assert "drill is NOT passed" in capsys.readouterr().err


@pytest.mark.parametrize("row_kwargs, message", [
    ({"writes": False}, "both levels enabled"),
    ({"public": False}, "both levels enabled"),
    ({"reason": "budget 100%"}, "not the operator clear"),
    ({"updated_at": NOW - 3600}, "predates this drill"),
])
def test_drill_row_that_does_not_prove_the_clear_fails(mig, repo, capsys, row_kwargs, message):
    code, _world, _http = _full_run(mig, repo, row=_row(mig=mig, **row_kwargs))
    assert code == 1
    assert message in capsys.readouterr().err


def test_drill_row_payload_that_is_malformed_fails(mig, repo, capsys):
    row = _row(mig=mig)
    row["Payload"] = json.dumps({"writes_enabled": "true", "public_enabled": True})
    code, _world, _http = _full_run(mig, repo, row=row)
    assert code == 1
    assert "payload is malformed" in capsys.readouterr().err


def test_drill_full_pass(mig, repo, capsys):
    code, world, http = _full_run(mig, repo)
    out = capsys.readouterr().out
    assert code == 0
    assert "PASS: the kill-switch row shows both levels enabled" in out
    (entity,) = _commands(world, "storage", "entity", "show")
    assert entity[entity.index("--partition-key") + 1] == "__wattracker_auth_v1__"
    assert entity[entity.index("--row-key") + 1] == mig.KILL_SWITCH_ROW_KEY
    assert entity[entity.index("--table-name") + 1] == "CloudControl"
    for step in range(5):
        assert f"| {step}. " in out and "| PASS |" in out
    assert "HTTP 200" in out


def test_drill_retries_a_cold_start_then_passes(mig, repo):
    world = FakeWorld(repo, mig)
    http = FakeHttp([(503, b""), (200, b'{"status":"ok"}')])
    assert _migrator(mig, repo, world, http=http, confirm_delete=APP).execute() == 0
    assert len(http.calls) == 2


def test_row_key_is_derived_from_the_source_module(mig):
    import hashlib

    expected = "kill-switch:" + hashlib.sha256(
        b"wattracker-cloud-kill-switch-v1\x00deployment"
    ).hexdigest()
    assert mig.KILL_SWITCH_ROW_KEY == expected
    assert mig.KILL_SWITCH_PARTITION == "__wattracker_auth_v1__"


def test_operator_clear_reason_matches_the_hook(mig):
    source = (ROOT / "wattracker/cloud/budget_hook.py").read_text()
    assert f'clear_kill_switch(backend, reason="{mig.OPERATOR_CLEAR_REASON}")' in source


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
    assert _migrator(mig, repo, world, from_step=4).execute() == 0
    assert not _commands(world, "appsettings", "set")
    assert not [call for call in world.calls if call[0] == "func"]
    assert _commands(world, "storage", "entity", "show")


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
