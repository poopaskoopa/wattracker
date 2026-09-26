#!/usr/bin/env python3
"""Run the #339 budget-hook Flex Consumption migration (DEPLOY.md sections 1-4).

Steps, each safe to re-run and resumable with ``--from-step N``:

0. preflight (read-only, always runs)
1. Flex Function: subnet, bootstrap storage, Y1 delete (confirmed), Flex create
2. parameters and deploy: rewrite three params, run scripts/deploy_cloud.py
3. settings and publish: app settings, stage, ``func ... publish``
4. drill: POST /budget/clear, then prove the CloudControl row landed

Every Azure/git/func call goes through one injectable runner.  Nothing here
prints parameter contents, credentials, host keys or response bodies, and the
budget-hook token is read only from ``WATTRACKER_BUDGET_HOOK_TOKEN``.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = SCRIPT_DIR.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

# The kill-switch row identity comes from the source, never a copy of it.
from wattracker.cloud.limits import (  # noqa: E402
    KILL_SWITCH_KEY,
    KILL_SWITCH_RECORD_KIND,
    KillSwitchUnavailable,
    _kill_switch_from_record,
)
from wattracker.cloud.security import (  # noqa: E402
    _AUTH_PARTITION,
    AzureTableSecurityStateBackend,
)


TOKEN_ENV = "WATTRACKER_BUDGET_HOOK_TOKEN"
DEPLOYMENT_PARAMETER = Path("infra/azure/main.local.bicepparam")
BACKUP_NAME = "main.pre-339-backup.local.bicepparam"
STAGED_DIR = Path("build/azure-budget-hook")
VNET_NAME = "wattracker-vnet"
SUBNET_NAME = "budget-hook-flex"
SUBNET_PREFIX = "10.42.2.0/27"
SUBNET_DELEGATION = "Microsoft.App/environments"
SUBNET_SERVICE_ENDPOINT = "Microsoft.Storage"
SUBNET_API_VERSION = "2023-11-01"  # the version main.bicep declares the subnet with
CONTROL_TABLE = "CloudControl"
BUDGET_HOOK_ROLE = "Wattracker Budget Hook Writer"
# budget_hook.py: clear_kill_switch(backend, reason="operator clear")
OPERATOR_CLEAR_REASON = "operator clear"
KILL_SWITCH_ROW_KEY = AzureTableSecurityStateBackend._row_key(
    KILL_SWITCH_RECORD_KIND, KILL_SWITCH_KEY
)
KILL_SWITCH_PARTITION = _AUTH_PARTITION
DRILL_OK_BODY = b'{"status":"ok"}'
# A row older than the drill (minus this clock-skew allowance) is a leftover
# from an earlier clear -- e.g. the 2026-09-19 defaultAction=Allow test -- and
# proves nothing about the new Function's network path.
ROW_FRESHNESS_SKEW_SECONDS = 300.0
REWRITTEN_PARAMS = ("budgetHookPrincipalId", "budgetHookHost", "budgetHookFunctionAppName")
DEAD_PARAMS = ("budgetHookIpRules",)
STEP_NAMES = {
    0: "preflight",
    1: "Flex Function",
    2: "parameters and deploy",
    3: "settings and publish",
    4: "drill",
}

_GUID_RE = re.compile(r"\A[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\Z")
_HOST_RE = re.compile(r"\A(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\Z")
_APP_NAME_RE = re.compile(r"\A[A-Za-z0-9](?:[A-Za-z0-9-]{0,58}[A-Za-z0-9])?\Z")
_STORAGE_NAME_RE = re.compile(r"\A[a-z0-9]{3,24}\Z")
_RG_RE = re.compile(r"\A[\w.()-]{1,90}\Z")
_LOCATION_RE = re.compile(r"\A[a-z0-9]{2,40}\Z")
_ENV_READ_RE = re.compile(rb"readEnvironmentVariable\(\s*'([A-Za-z_][A-Za-z0-9_]*)'\s*\)")


def _param_decl_re(name: str) -> re.Pattern[bytes]:
    return re.compile(rb"(?m)^[ \t]*param[ \t]+" + re.escape(name.encode()) + rb"\b")


def _param_literal_re(name: str) -> re.Pattern[bytes]:
    return re.compile(
        rb"(?m)^[ \t]*param[ \t]+" + re.escape(name.encode())
        + rb"[ \t]*=[ \t]*'(?P<value>[^'\\\r\n]*)'[ \t]*(?://[^\r\n]*)?(?:\r?\n|\Z)"
    )


class MigrationError(RuntimeError):
    """A safe, user-facing failure. Messages never carry secrets or bodies."""


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "MISSING"


MISSING = _Missing()


# ---------------------------------------------------------------------------
# Injectable side effects
# ---------------------------------------------------------------------------

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
HttpPost = Callable[[str, Mapping[str, str], float], "tuple[int, bytes]"]


def default_runner(
    argv: Sequence[str], *, cwd: Path | None = None, capture: bool = True
) -> "subprocess.CompletedProcess[str]":
    """Run one command. Captured output is returned, never echoed.

    The budget-hook token is removed from every child's environment: no child
    needs it (it reaches Azure only via a 0600 ``--settings @file``).
    """

    env = {key: value for key, value in os.environ.items() if key != TOKEN_ENV}
    kwargs: dict[str, Any] = {"cwd": cwd, "check": False, "env": env}
    if capture:
        kwargs.update(capture_output=True, text=True, stdin=subprocess.DEVNULL)
    try:
        return subprocess.run(list(argv), **kwargs)
    except FileNotFoundError:
        raise MigrationError(f"{Path(argv[0]).name} is not installed or not on PATH") from None
    except OSError:
        raise MigrationError(f"could not run {Path(argv[0]).name}") from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: it would re-send the host key to another URL."""

    def redirect_request(self, *_args, **_kwargs):  # type: ignore[override]
        return None


def default_http_post(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
    if not url.startswith("https://"):
        raise MigrationError("refusing a non-HTTPS drill URL")
    opener = urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(url, data=b"", method="POST", headers=dict(headers))
    try:
        with opener.open(request, timeout=timeout) as response:
            return int(response.status), response.read(4096)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(4096)
        except OSError:
            body = b""
        finally:
            exc.close()
        return int(exc.code), body
    except (urllib.error.URLError, TimeoutError, OSError):
        raise MigrationError("could not reach the budget hook over HTTPS") from None


# ---------------------------------------------------------------------------
# Options and validation
# ---------------------------------------------------------------------------


@dataclass
class Options:
    resource_group: str
    subscription: str
    function_app_name: str
    bootstrap_storage_name: str
    params: Path
    location: str = "eastus2"
    dry_run: bool = False
    from_step: int = 1
    confirm_delete: str | None = None

    def validate(self) -> None:
        if not _RG_RE.fullmatch(self.resource_group) or self.resource_group.startswith("-"):
            raise MigrationError("invalid --resource-group")
        if not _GUID_RE.fullmatch(self.subscription):
            raise MigrationError("--subscription must be the subscription ID (a GUID)")
        if not _LOCATION_RE.fullmatch(self.location):
            raise MigrationError("invalid --location; use the short form, e.g. eastus2")
        if not _APP_NAME_RE.fullmatch(self.function_app_name):
            raise MigrationError("invalid --function-app-name")
        if not _STORAGE_NAME_RE.fullmatch(self.bootstrap_storage_name):
            raise MigrationError("invalid --bootstrap-storage-name")
        if self.from_step not in (1, 2, 3, 4):
            raise MigrationError("--from-step must be 1, 2, 3 or 4")
        if self.confirm_delete is not None and self.confirm_delete != self.function_app_name:
            raise MigrationError(
                "--confirm-delete does not match --function-app-name; nothing was deleted"
            )

    def resume_command(self, step: int) -> str:
        argv = [
            ".venv/bin/python", "scripts/migrate_budget_hook.py",
            "--resource-group", self.resource_group,
            "--subscription", self.subscription,
            "--location", self.location,
            "--function-app-name", self.function_app_name,
            "--bootstrap-storage-name", self.bootstrap_storage_name,
            "--params", str(self.params),
        ]
        if self.confirm_delete is not None:
            argv += ["--confirm-delete", self.confirm_delete]
        argv += ["--from-step", str(max(step, 1))]
        return shlex.join(argv)


@dataclass
class StepRecord:
    step: int
    result: str
    started: str
    finished: str


@dataclass
class Facts:
    principal_id: str = ""
    host: str = ""
    subnet_id: str = ""
    row_updated_at: str = ""


def _utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _same_id(left: object, right: object) -> bool:
    return isinstance(left, str) and isinstance(right, str) and left.lower() == right.lower()


# ---------------------------------------------------------------------------
# The parameter file
# ---------------------------------------------------------------------------


def check_param_structure(contents: bytes) -> None:
    """Refuse a file the three-value edit (or the deployment) cannot handle."""

    for name in (*REWRITTEN_PARAMS, "storageName"):
        declarations = len(_param_decl_re(name).findall(contents))
        if declarations == 0:
            raise MigrationError(f"parameter file does not declare {name}")
        if declarations > 1:
            raise MigrationError(f"parameter file declares {name} more than once")
        if not _param_literal_re(name).search(contents):
            raise MigrationError(f"parameter {name} must be a single-quoted literal on one line")
    for name in DEAD_PARAMS:
        if _param_decl_re(name).search(contents):
            raise MigrationError(
                f"parameter file still declares {name}; delete that whole param block by "
                "hand first (DEPLOY.md section 2: main.bicep no longer declares it, BCP259)"
            )


def read_param(contents: bytes, name: str) -> str:
    check_param_structure(contents)
    match = _param_literal_re(name).search(contents)
    assert match is not None
    return match.group("value").decode("utf-8")


def rewrite_params(contents: bytes, values: Mapping[str, str]) -> bytes:
    """Replace only the value spans of the three budget-hook params."""

    check_param_structure(contents)
    if set(values) != set(REWRITTEN_PARAMS):
        raise MigrationError("internal error: unexpected parameter set")
    spans = []
    for name, value in values.items():
        if "'" in value or "\\" in value or any(ord(c) < 0x20 for c in value):
            raise MigrationError(f"refusing an unsafe value for {name}")
        match = _param_literal_re(name).search(contents)
        assert match is not None
        spans.append((match.start("value"), match.end("value"), value.encode("utf-8")))
    updated = bytearray(contents)
    for start, end, value in sorted(spans, reverse=True):
        updated[start:end] = value
    return bytes(updated)


def _atomic_write(path: Path, contents: bytes, mode: int) -> None:
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
        temporary_name = None
    except OSError:
        raise MigrationError("could not update the parameter file") from None
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------


class Migrator:
    def __init__(
        self,
        options: Options,
        *,
        runner: Runner = default_runner,
        http_post: HttpPost = default_http_post,
        environ: Mapping[str, str] | None = None,
        prompt: Callable[[str], str] = input,
        stdin_isatty: Callable[[], bool] = lambda: sys.stdin.isatty(),
        which: Callable[[str], str | None] = shutil.which,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
        repo_root: Path = REPOSITORY_ROOT,
        python: str = sys.executable,
    ) -> None:
        self.o = options
        self.runner = runner
        self.http_post = http_post
        self.environ = os.environ if environ is None else environ
        self.prompt = prompt
        self.stdin_isatty = stdin_isatty
        self.which = which
        self.sleep = sleep
        self.now = now
        self.repo = repo_root.resolve()
        self.python = python
        self.facts = Facts()
        self.records: list[StepRecord] = []
        self.drill_status: int | None = None

    # -- paths and ids ---------------------------------------------------

    @property
    def params_path(self) -> Path:
        return self.o.params

    @property
    def vnet_id(self) -> str:
        return (
            f"/subscriptions/{self.o.subscription}/resourceGroups/{self.o.resource_group}"
            f"/providers/Microsoft.Network/virtualNetworks/{VNET_NAME}"
        )

    @property
    def expected_subnet_id(self) -> str:
        return f"{self.vnet_id}/subnets/{SUBNET_NAME}"

    # -- command builders (shared by the real run and --dry-run) ----------

    def _az(self, *args: str, pin: bool = True) -> list[str]:
        argv = ["az", *args]
        if pin:
            argv += ["--subscription", self.o.subscription]
        return argv + ["--output", "json"]

    def _rg(self) -> list[str]:
        return ["--resource-group", self.o.resource_group]

    def _app(self) -> list[str]:
        return ["--name", self.o.function_app_name, *self._rg()]

    def cmd_account_show(self) -> list[str]:
        return self._az("account", "show", pin=False)

    def cmd_group_show(self) -> list[str]:
        return self._az("group", "show", "--name", self.o.resource_group)

    def cmd_flex_locations(self) -> list[str]:
        return self._az("functionapp", "list-flexconsumption-locations")

    def cmd_provider_show(self) -> list[str]:
        return self._az("provider", "show", "--namespace", "Microsoft.App")

    def cmd_vnet_show(self) -> list[str]:
        return self._az("network", "vnet", "show", "--name", VNET_NAME, *self._rg())

    def cmd_subnet_show(self) -> list[str]:
        return self._az(
            "network", "vnet", "subnet", "show", "--name", SUBNET_NAME,
            "--vnet-name", VNET_NAME, *self._rg(),
        )

    def subnet_body(self) -> dict[str, Any]:
        # Exactly the budgetHookSubnet properties in main.bicep, including the
        # delegation *name*: the CLI's `subnet create --delegations` names it
        # "0", and main.bicep renaming the delegation of an in-use subnet at
        # step 2 is a change Azure can refuse.
        return {
            "properties": {
                "addressPrefix": SUBNET_PREFIX,
                "delegations": [
                    {"name": SUBNET_NAME, "properties": {"serviceName": SUBNET_DELEGATION}}
                ],
                "serviceEndpoints": [
                    {"service": SUBNET_SERVICE_ENDPOINT, "locations": [self.o.location]}
                ],
            }
        }

    def cmd_subnet_put(self) -> list[str]:
        url = (
            f"https://management.azure.com{self.expected_subnet_id}"
            f"?api-version={SUBNET_API_VERSION}"
        )
        return self._az(
            "rest", "--method", "put", "--url", url,
            "--body", json.dumps(self.subnet_body(), separators=(",", ":")),
            pin=False,
        )

    def cmd_bootstrap_storage_show(self) -> list[str]:
        return self._az(
            "storage", "account", "show", "--name", self.o.bootstrap_storage_name, *self._rg()
        )

    def cmd_bootstrap_storage_create(self) -> list[str]:
        return self._az(
            "storage", "account", "create", "--name", self.o.bootstrap_storage_name,
            *self._rg(), "--location", self.o.location, "--sku", "Standard_LRS",
        )

    def cmd_app_show(self) -> list[str]:
        return self._az("functionapp", "show", *self._app())

    def cmd_app_delete(self) -> list[str]:
        return self._az("functionapp", "delete", *self._app())

    def cmd_app_create(self, subnet_id: str) -> list[str]:
        return self._az(
            "functionapp", "create", *self._app(),
            "--storage-account", self.o.bootstrap_storage_name,
            "--flexconsumption-location", self.o.location,
            "--runtime", "python", "--runtime-version", "3.12",
            "--functions-version", "4",
            "--vnet", self.vnet_id, "--subnet", SUBNET_NAME,
        )

    def cmd_identity_show(self) -> list[str]:
        return self._az("functionapp", "identity", "show", *self._app())

    def cmd_identity_assign(self) -> list[str]:
        return self._az("functionapp", "identity", "assign", *self._app())

    def cmd_deploy_cloud(self) -> list[str]:
        return [
            self.python, str(self.repo / "scripts" / "deploy_cloud.py"),
            str(self.params_path), "--resource-group", self.o.resource_group,
            "--always-deploy",
        ]

    def cmd_app_storage_show(self, storage_name: str) -> list[str]:
        return self._az("storage", "account", "show", "--name", storage_name, *self._rg())

    def cmd_role_list(self, scope: str) -> list[str]:
        return self._az("role", "assignment", "list", "--scope", scope)

    def cmd_appsettings_set(self, settings_file: str) -> list[str]:
        return self._az(
            "functionapp", "config", "appsettings", "set", *self._app(),
            "--settings", f"@{settings_file}",
        )

    def cmd_package(self) -> list[str]:
        return [self.python, str(self.repo / "scripts" / "package_budget_hook.py")]

    def cmd_publish(self) -> list[str]:
        return ["func", "azure", "functionapp", "publish", self.o.function_app_name, "--python"]

    def cmd_keys_list(self) -> list[str]:
        return self._az("functionapp", "keys", "list", *self._app())

    def cmd_entity_show(self, storage_name: str) -> list[str]:
        return self._az(
            "storage", "entity", "show", "--account-name", storage_name,
            "--table-name", CONTROL_TABLE, "--auth-mode", "login",
            "--partition-key", KILL_SWITCH_PARTITION, "--row-key", KILL_SWITCH_ROW_KEY,
            "--select", "PartitionKey", "RowKey", "Payload", "Timestamp",
        )

    # -- running -----------------------------------------------------------

    @staticmethod
    def _label(argv: Sequence[str]) -> str:
        words = []
        for arg in argv:
            if arg.startswith("-"):
                break
            words.append(Path(arg).name if not words else arg)
        return " ".join(words[:5])

    def run(self, argv: Sequence[str], *, cwd: Path | None = None, capture: bool = True):
        if self.o.dry_run:  # defence in depth: a dry run never reaches the runner
            raise MigrationError("internal error: a command was run during --dry-run")
        return self.runner(list(argv), cwd=cwd if cwd is not None else self.repo, capture=capture)

    def run_ok(self, argv: Sequence[str], **kwargs: Any) -> "subprocess.CompletedProcess[str]":
        result = self.run(argv, **kwargs)
        if result.returncode:
            raise MigrationError(
                f"`{self._label(argv)}` failed (exit {result.returncode}); its output is "
                "suppressed here, so re-run that command by hand to see the diagnostics"
            )
        return result

    def az_json(self, argv: Sequence[str], *, allow_missing: bool = False) -> Any:
        result = self.run(argv)
        if allow_missing and result.returncode == 3:  # az: resource not found
            return MISSING
        if result.returncode:
            raise MigrationError(
                f"`{self._label(argv)}` failed (exit {result.returncode}); its output is "
                "suppressed here, so re-run that command by hand to see the diagnostics"
            )
        text = (result.stdout or "").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise MigrationError(f"`{self._label(argv)}` returned unreadable JSON") from None

    def say(self, message: str) -> None:
        print(message, flush=True)

    # -- step 0 ------------------------------------------------------------

    def read_params_bytes(self) -> bytes:
        path = self.params_path
        try:
            if path.is_symlink() or not path.is_file():
                raise MigrationError("--params must be an existing regular file")
            return path.read_bytes()
        except OSError:
            raise MigrationError("could not read the parameter file") from None

    def storage_name(self) -> str:
        name = read_param(self.read_params_bytes(), "storageName")
        if not _STORAGE_NAME_RE.fullmatch(name):
            raise MigrationError("storageName in the parameter file is not a storage account name")
        return name

    def local_preflight(self) -> None:
        """Checks that need no subprocess, so --dry-run performs them too."""

        self.o.validate()
        self.o.params = self.o.params.expanduser().absolute()
        try:
            relative = self.o.params.resolve().relative_to(self.repo)
        except ValueError:
            raise MigrationError("--params must be inside this checkout") from None
        if relative != DEPLOYMENT_PARAMETER:
            raise MigrationError(
                "--params must be infra/azure/main.local.bicepparam "
                "(the only file scripts/deploy_cloud.py will deploy)"
            )
        contents = self.read_params_bytes()
        check_param_structure(contents)
        self.storage_name()
        if not self.environ.get(TOKEN_ENV):
            raise MigrationError(f"{TOKEN_ENV} must be set in the environment (never argv)")
        if self.o.from_step <= 2:
            missing = [
                name.decode() for name in dict.fromkeys(_ENV_READ_RE.findall(contents))
                if not self.environ.get(name.decode())
            ]
            if missing:
                raise MigrationError(
                    "the parameter file reads these unset environment variables: "
                    + ", ".join(missing)
                )
        for tool in ("az", "func", "git"):
            if not self.which(tool):
                raise MigrationError(f"{tool} is not installed or not on PATH")
        if self.o.from_step <= 3 and (self.repo / STAGED_DIR).exists():
            raise MigrationError(
                f"{STAGED_DIR} already exists; remove only that directory yourself "
                f"(rm -rf -- {STAGED_DIR}) -- this script never deletes it"
            )

    def step0_preflight(self) -> None:
        # The checkout: the params file is untracked and ignored, and
        # deploy_cloud.py's own clean-main guard will pass.
        git = ["git", "-C", str(self.repo)]
        relative = str(DEPLOYMENT_PARAMETER)
        if self.run(git + ["ls-files", "--error-unmatch", "--", relative]).returncode == 0:
            raise MigrationError("refusing a tracked parameter file; it must stay untracked")
        for candidate in (relative, str(DEPLOYMENT_PARAMETER.parent / BACKUP_NAME)):
            if self.run(git + ["check-ignore", "-q", "--", candidate]).returncode != 0:
                raise MigrationError(f"{candidate} is not git-ignored; refusing to write it")
        branch = self.run_ok(git + ["branch", "--show-current"]).stdout.strip()
        if branch != "main":
            raise MigrationError("run this from a checkout of main (deploy_cloud.py requires it)")
        status = self.run_ok(git + ["status", "--porcelain=v1", "--untracked-files=all"])
        if status.stdout.strip():
            raise MigrationError("the working tree is dirty; deploy_cloud.py would refuse it")

        account = self.az_json(self.cmd_account_show())
        if not isinstance(account, dict) or not _same_id(account.get("id"), self.o.subscription):
            raise MigrationError(
                "the active az subscription is not --subscription; run "
                "`az account set --subscription <id>` first (func publishes to it)"
            )
        if self.az_json(self.cmd_group_show(), allow_missing=True) is MISSING:
            raise MigrationError("the resource group does not exist")
        locations = self.az_json(self.cmd_flex_locations()) or []
        names = {
            str(item.get("name", "")).replace(" ", "").lower()
            for item in locations if isinstance(item, dict)
        }
        if self.o.location not in names:
            raise MigrationError(f"{self.o.location} is not a Flex Consumption location")
        provider = self.az_json(self.cmd_provider_show())
        if not isinstance(provider, dict) or provider.get("registrationState") != "Registered":
            raise MigrationError("the Microsoft.App provider is not registered")
        vnet = self.az_json(self.cmd_vnet_show(), allow_missing=True)
        if vnet is MISSING or not isinstance(vnet, dict):
            raise MigrationError(
                f"{VNET_NAME} does not exist. This script never creates or rewrites the "
                "VNet (vnet create against a live network can drop subnets); stop and "
                "investigate"
            )
        prefixes = ((vnet.get("addressSpace") or {}).get("addressPrefixes")) or []
        wanted = ipaddress.ip_network(SUBNET_PREFIX)
        contained = False
        for prefix in prefixes:
            try:
                network = ipaddress.ip_network(str(prefix), strict=False)
            except ValueError:
                continue
            if network.version == wanted.version and wanted.subnet_of(network):
                contained = True
        if not contained:
            raise MigrationError(f"{VNET_NAME}'s address space does not contain {SUBNET_PREFIX}")

    # -- step 1 ------------------------------------------------------------

    def verify_subnet(self, subnet: Mapping[str, Any]) -> None:
        prefixes = [subnet.get("addressPrefix"), *(subnet.get("addressPrefixes") or [])]
        if SUBNET_PREFIX not in [p for p in prefixes if p]:
            raise MigrationError(f"{SUBNET_NAME} exists but its prefix is not {SUBNET_PREFIX}")
        delegations = subnet.get("delegations") or []
        services = [d.get("serviceName") for d in delegations if isinstance(d, dict)]
        if services != [SUBNET_DELEGATION]:
            raise MigrationError(
                f"{SUBNET_NAME} exists but is not delegated only to {SUBNET_DELEGATION}"
            )
        endpoints = [
            e.get("service") for e in subnet.get("serviceEndpoints") or [] if isinstance(e, dict)
        ]
        if SUBNET_SERVICE_ENDPOINT not in endpoints:
            raise MigrationError(
                f"{SUBNET_NAME} exists but has no {SUBNET_SERVICE_ENDPOINT} service endpoint"
            )
        if [d.get("name") for d in delegations] != [SUBNET_NAME]:
            self.say(
                f"warning: {SUBNET_NAME}'s delegation is not named {SUBNET_NAME!r} as in "
                "main.bicep; step 2 will rename it, which Azure may refuse on an in-use subnet"
            )

    def ensure_subnet(self) -> str:
        subnet = self.az_json(self.cmd_subnet_show(), allow_missing=True)
        if subnet is MISSING:
            self.say(f"creating subnet {SUBNET_NAME} ({SUBNET_PREFIX}) with the main.bicep body")
            self.run_ok(self.cmd_subnet_put())
            for _attempt in range(20):
                subnet = self.az_json(self.cmd_subnet_show(), allow_missing=True)
                if isinstance(subnet, dict) and subnet.get("provisioningState") == "Succeeded":
                    break
                self.sleep(3)
            else:
                raise MigrationError(f"{SUBNET_NAME} did not finish provisioning")
        else:
            self.say(f"subnet {SUBNET_NAME} exists; verifying it")
        if not isinstance(subnet, dict):
            raise MigrationError(f"could not read {SUBNET_NAME}")
        self.verify_subnet(subnet)
        subnet_id = subnet.get("id")
        if not _same_id(subnet_id, self.expected_subnet_id):
            raise MigrationError(f"{SUBNET_NAME} has an unexpected resource ID")
        self.facts.subnet_id = str(subnet_id)
        return str(subnet_id)

    @staticmethod
    def is_flex(app: Mapping[str, Any]) -> bool:
        sku = str(app.get("sku") or "").lower()
        return sku == "flexconsumption" or isinstance(app.get("functionAppConfig"), dict)

    def app_subnet(self, app: Mapping[str, Any]) -> object:
        return app.get("virtualNetworkSubnetId") or (app.get("siteConfig") or {}).get(
            "virtualNetworkSubnetId"
        )

    def confirm_delete(self, app: Mapping[str, Any]) -> None:
        name = self.o.function_app_name
        description = f"kind={app.get('kind')!s}, sku={app.get('sku')!s}"
        self.say(
            f"{name} exists and is NOT a Flex Consumption app ({description}).\n"
            "Deleting it is the one destructive step: it removes the old Y1 site, its "
            "host key and its system identity."
        )
        if self.o.confirm_delete is not None:
            if self.o.confirm_delete != name:  # validate() already refuses this
                raise MigrationError("--confirm-delete does not match; nothing was deleted")
            return
        if not self.stdin_isatty():
            raise MigrationError(
                "deleting the app needs confirmation: run interactively, or pass "
                f"--confirm-delete {name}"
            )
        try:
            typed = self.prompt(f"Type the Function App name ({name}) to delete it: ")
        except EOFError:
            typed = ""
        if typed.strip() != name:
            raise MigrationError("confirmation did not match; nothing was deleted")

    def step1_flex(self) -> None:
        subnet_id = self.ensure_subnet()
        storage = self.az_json(self.cmd_bootstrap_storage_show(), allow_missing=True)
        if storage is MISSING:
            self.say(f"creating bootstrap storage {self.o.bootstrap_storage_name}")
            self.run_ok(self.cmd_bootstrap_storage_create())
        else:
            self.say(f"bootstrap storage {self.o.bootstrap_storage_name} exists; skipping")

        app = self.az_json(self.cmd_app_show(), allow_missing=True)
        if app is not MISSING and not isinstance(app, dict):
            raise MigrationError("could not read the Function App")
        if isinstance(app, dict) and self.is_flex(app):
            self.say(f"{self.o.function_app_name} is already Flex Consumption; skipping delete/create")
        else:
            if isinstance(app, dict):
                self.confirm_delete(app)
                self.say(f"deleting {self.o.function_app_name}")
                self.run_ok(self.cmd_app_delete())
            self.say(f"creating Flex Function App {self.o.function_app_name}")
            self.run_ok(self.cmd_app_create(subnet_id))

        identity = self.az_json(self.cmd_identity_show(), allow_missing=True)
        if not isinstance(identity, dict) or not identity.get("principalId"):
            self.say("assigning the system identity")
            self.run_ok(self.cmd_identity_assign())
        self.read_app_facts()

    def read_app_facts(self) -> None:
        app = self.az_json(self.cmd_app_show(), allow_missing=True)
        if not isinstance(app, dict):
            raise MigrationError(
                f"{self.o.function_app_name} does not exist; resume with --from-step 1"
            )
        if not self.is_flex(app):
            raise MigrationError(
                f"{self.o.function_app_name} is not Flex Consumption; resume with --from-step 1"
            )
        if not _same_id(self.app_subnet(app), self.expected_subnet_id):
            raise MigrationError(
                f"{self.o.function_app_name} is not integrated with {SUBNET_NAME}; integrate it "
                "(or delete it by hand) and resume with --from-step 1"
            )
        host = str(app.get("defaultHostName") or "").lower()
        if not _HOST_RE.fullmatch(host):
            raise MigrationError("the Function App has no valid defaultHostName")
        identity = self.az_json(self.cmd_identity_show(), allow_missing=True)
        principal = identity.get("principalId") if isinstance(identity, dict) else None
        if not isinstance(principal, str) or not _GUID_RE.fullmatch(principal):
            raise MigrationError("the Function App has no system-assigned identity principalId")
        self.facts.principal_id = principal.lower()
        self.facts.host = host
        self.facts.subnet_id = str(self.app_subnet(app))
        self.say(f"Function App: principalId={self.facts.principal_id} host={host}")

    def ensure_facts(self) -> None:
        if not (self.facts.principal_id and self.facts.host):
            self.read_app_facts()

    # -- step 2 ------------------------------------------------------------

    def write_backup(self, original: bytes) -> None:
        backup = self.params_path.parent / BACKUP_NAME
        if backup.exists() or backup.is_symlink():
            self.say(f"keeping the existing backup {backup.name}")
            return
        try:
            descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(original)
            os.chmod(backup, 0o600)
        except OSError:
            raise MigrationError("could not write the parameter backup") from None
        self.say(f"backed up the parameter file to {backup.name} (0600)")

    def update_params(self) -> None:
        original = self.read_params_bytes()
        values = {
            "budgetHookPrincipalId": self.facts.principal_id,
            "budgetHookHost": self.facts.host,
            "budgetHookFunctionAppName": self.o.function_app_name,
        }
        updated = rewrite_params(original, values)
        if updated == original:
            self.say("parameter file already has the Function's values; not rewriting")
            return
        mode = stat.S_IMODE(self.params_path.stat().st_mode)
        self.write_backup(original)
        _atomic_write(self.params_path, updated, mode)
        self.say("updated budgetHookPrincipalId, budgetHookHost, budgetHookFunctionAppName")

    def verify_network_and_role(self) -> None:
        storage = self.az_json(self.cmd_app_storage_show(self.storage_name()), allow_missing=True)
        if not isinstance(storage, dict):
            raise MigrationError("could not read the application storage account")
        rules = storage.get("networkRuleSet") or {}
        if str(rules.get("defaultAction", "")).lower() != "deny":
            raise MigrationError(
                "application storage defaultAction is not Deny; the drill would prove nothing"
            )
        if rules.get("ipRules"):
            raise MigrationError("application storage still has IP rules; the Function must not depend on them")
        vnet_rules = rules.get("virtualNetworkRules") or []
        if not any(
            isinstance(rule, dict)
            and (
                _same_id(rule.get("virtualNetworkResourceId"), self.expected_subnet_id)
                or _same_id(rule.get("id"), self.expected_subnet_id)
            )
            and str(rule.get("action", "Allow")).lower() == "allow"
            for rule in vnet_rules
        ):
            raise MigrationError(
                f"application storage has no virtualNetworkRules entry for {SUBNET_NAME}"
            )
        self.say(f"storage firewall: Deny, no IP rules, {SUBNET_NAME} virtual-network rule present")

        storage_id = storage.get("id")
        if not isinstance(storage_id, str) or not storage_id.startswith("/subscriptions/"):
            raise MigrationError("the application storage account has no resource ID")
        scope = f"{storage_id}/tableServices/default/tables/{CONTROL_TABLE}"
        assignments = self.az_json(self.cmd_role_list(scope)) or []
        if not any(
            isinstance(item, dict)
            and _same_id(item.get("principalId"), self.facts.principal_id)
            and item.get("roleDefinitionName", BUDGET_HOOK_ROLE) == BUDGET_HOOK_ROLE
            for item in assignments
        ):
            raise MigrationError(
                f"the new principal has no {BUDGET_HOOK_ROLE} assignment on {CONTROL_TABLE}; "
                "the main deployment did not reach Azure"
            )
        self.say(f"{BUDGET_HOOK_ROLE} is assigned to the new principal on {CONTROL_TABLE}")

    def step2_deploy(self) -> None:
        self.ensure_facts()
        self.update_params()
        self.say("running scripts/deploy_cloud.py (its output follows)")
        result = self.run(self.cmd_deploy_cloud(), capture=False)
        if result.returncode:
            raise MigrationError("scripts/deploy_cloud.py failed; see its output above")
        self.verify_network_and_role()

    # -- step 3 ------------------------------------------------------------

    def set_app_settings(self, storage_name: str) -> None:
        token = self.environ.get(TOKEN_ENV) or ""
        if not token:
            raise MigrationError(f"{TOKEN_ENV} is not set")
        settings = [
            {"name": "WATTRACKER_STORAGE_ACCOUNT_NAME", "value": storage_name, "slotSetting": False},
            {"name": TOKEN_ENV, "value": token, "slotSetting": False},
        ]
        path: str | None = None
        try:
            descriptor, path = tempfile.mkstemp(prefix="wattracker-settings-", suffix=".json")
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(settings, handle)
            self.run_ok(self.cmd_appsettings_set(path))
        except OSError:
            raise MigrationError("could not write the temporary settings file") from None
        finally:
            if path is not None:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
        self.say("set WATTRACKER_STORAGE_ACCOUNT_NAME and WATTRACKER_BUDGET_HOOK_TOKEN")

    def step3_publish(self) -> None:
        self.ensure_facts()
        staged = self.repo / STAGED_DIR
        if staged.exists():
            raise MigrationError(
                f"{STAGED_DIR} already exists; remove only that directory yourself "
                f"(rm -rf -- {STAGED_DIR}) -- this script never deletes it"
            )
        self.set_app_settings(self.storage_name())
        self.say("staging the hook")
        self.run_ok(self.cmd_package())
        if not staged.is_dir():
            raise MigrationError(f"{STAGED_DIR} was not created")
        self.say("publishing with func (a remote build; this can take minutes, output captured)")
        self.run_ok(self.cmd_publish(), cwd=staged)
        self.say("published")

    # -- step 4 ------------------------------------------------------------

    def host_key(self) -> str:
        keys = self.az_json(self.cmd_keys_list())
        key = ((keys or {}).get("functionKeys") or {}).get("default") if isinstance(keys, dict) else None
        if not isinstance(key, str) or not key:
            raise MigrationError("the Function App has no default function key")
        return key

    def post_clear(self) -> tuple[int, bytes]:
        token = self.environ.get(TOKEN_ENV) or ""
        if not token:
            raise MigrationError(f"{TOKEN_ENV} is not set")
        key = self.host_key()
        url = f"https://{self.facts.host}/budget/clear"
        headers = {"x-functions-key": key, "X-Wattracker-Budget-Token": token}
        status, body = 0, b""
        try:
            for attempt in range(1, 4):
                try:
                    status, body = self.http_post(url, headers, 60.0)
                except MigrationError:
                    status, body = 0, b""
                    if attempt == 3:
                        raise
                if status not in (0, 502, 503, 504):
                    break
                if attempt < 3:
                    self.say(f"drill attempt {attempt}: HTTP {status or 'no response'}; retrying")
                    self.sleep(10)
        finally:
            headers.clear()
            del key
        return status, body

    def read_row(self, storage_name: str) -> dict[str, Any]:
        result = self.run(self.cmd_entity_show(storage_name))
        stderr = result.stderr or ""
        if result.returncode == 3 or "ResourceNotFound" in stderr or "does not exist" in stderr:
            raise MigrationError(
                "FAIL: HTTP 200 but the CloudControl kill-switch row does not exist"
            )
        if result.returncode:
            if "Authorization" in stderr or "not authorized" in stderr.lower():
                raise MigrationError(
                    "could not read the CloudControl row from this workstation (storage "
                    "firewall defaultAction=Deny or missing Storage Table Data Reader); the "
                    "drill is NOT passed until the row is proven"
                )
            raise MigrationError(
                "`az storage entity show` failed; the drill is NOT passed until the row is proven"
            )
        try:
            entity = json.loads(result.stdout or "")
        except json.JSONDecodeError:
            raise MigrationError("the CloudControl row query returned unreadable JSON") from None
        if not isinstance(entity, dict) or not entity:
            raise MigrationError("FAIL: HTTP 200 but the CloudControl kill-switch row does not exist")
        return entity

    def check_row(self, entity: Mapping[str, Any], drill_started: float) -> None:
        if entity.get("PartitionKey") != KILL_SWITCH_PARTITION or entity.get("RowKey") != KILL_SWITCH_ROW_KEY:
            raise MigrationError("FAIL: the returned entity is not the kill-switch row")
        try:
            record = json.loads(str(entity.get("Payload")))
            state = _kill_switch_from_record(record)
        except (json.JSONDecodeError, KillSwitchUnavailable):
            raise MigrationError("FAIL: the kill-switch row payload is malformed") from None
        if not (state.writes_enabled and state.public_enabled):
            raise MigrationError("FAIL: the kill-switch row does not show both levels enabled")
        if state.reason != OPERATOR_CLEAR_REASON:
            raise MigrationError("FAIL: the kill-switch row reason is not the operator clear")
        if state.updated_at < drill_started - ROW_FRESHNESS_SKEW_SECONDS:
            raise MigrationError(
                "FAIL: the kill-switch row predates this drill; it is a leftover, not proof"
            )
        self.facts.row_updated_at = _utc(state.updated_at)

    def step4_drill(self) -> None:
        self.ensure_facts()
        storage_name = self.storage_name()
        drill_started = self.now()
        self.say(f"POST https://{self.facts.host}/budget/clear (host key and token masked)")
        status, body = self.post_clear()
        self.drill_status = status
        if status != 200:
            raise MigrationError(f"FAIL: /budget/clear returned HTTP {status or 'no response'}")
        if body != DRILL_OK_BODY:
            raise MigrationError('FAIL: /budget/clear returned 200 but not exactly {"status":"ok"}')
        self.say('HTTP 200 {"status":"ok"}; reading the CloudControl row')
        self.check_row(self.read_row(storage_name), drill_started)
        self.say("PASS: the kill-switch row shows both levels enabled by the operator clear")

    # -- orchestration -------------------------------------------------------

    STEPS = {
        1: "step1_flex",
        2: "step2_deploy",
        3: "step3_publish",
        4: "step4_drill",
    }

    def print_plan(self) -> None:
        mask = "***"
        storage = "<storageName from params>"
        host = "<defaultHostName from Azure>"
        say = self.say
        say("dry-run: no subprocess or network call is made; the commands a real run may issue:")
        say("\n[step 0: preflight]")
        git = ["git", "-C", str(self.repo)]
        for argv in (
            git + ["ls-files", "--error-unmatch", "--", str(DEPLOYMENT_PARAMETER)],
            git + ["check-ignore", "-q", "--", str(DEPLOYMENT_PARAMETER)],
            git + ["check-ignore", "-q", "--", str(DEPLOYMENT_PARAMETER.parent / BACKUP_NAME)],
            git + ["branch", "--show-current"],
            git + ["status", "--porcelain=v1", "--untracked-files=all"],
            self.cmd_account_show(), self.cmd_group_show(), self.cmd_flex_locations(),
            self.cmd_provider_show(), self.cmd_vnet_show(),
        ):
            say("  " + shlex.join(argv))
        say("  (never `az network vnet create`: a missing VNet stops the run)")
        if self.o.from_step <= 1:
            say("\n[step 1: Flex Function]")
            say("  " + shlex.join(self.cmd_subnet_show()))
            say("  if the subnet is missing: " + shlex.join(self.cmd_subnet_put()))
            say("  " + shlex.join(self.cmd_bootstrap_storage_show()))
            say("  if missing: " + shlex.join(self.cmd_bootstrap_storage_create()))
            say("  " + shlex.join(self.cmd_app_show()))
            say(
                "  if the app exists and is not Flex, after the typed name confirmation: "
                + shlex.join(self.cmd_app_delete())
            )
            say("  unless already Flex: " + shlex.join(self.cmd_app_create(self.expected_subnet_id)))
            say("  " + shlex.join(self.cmd_identity_show()))
            say("  if no identity: " + shlex.join(self.cmd_identity_assign()))
        if self.o.from_step <= 2:
            say("\n[step 2: parameters and deploy]")
            say(
                "  rewrite budgetHookPrincipalId, budgetHookHost, budgetHookFunctionAppName in "
                f"{DEPLOYMENT_PARAMETER} (backup: {BACKUP_NAME}, 0600)"
            )
            say("  " + shlex.join(self.cmd_deploy_cloud()))
            say("  " + shlex.join(self.cmd_app_storage_show(storage)))
            say("  " + shlex.join(self.cmd_role_list(f"<storage id>/tableServices/default/tables/{CONTROL_TABLE}")))
        if self.o.from_step <= 3:
            say("\n[step 3: settings and publish]")
            say(
                "  " + shlex.join(self.cmd_appsettings_set("<0600 temp file>"))
                + f"   # file: WATTRACKER_STORAGE_ACCOUNT_NAME={storage}, {TOKEN_ENV}={mask}; "
                "deleted afterwards"
            )
            say("  " + shlex.join(self.cmd_package()))
            say(f"  (cd {STAGED_DIR} && " + shlex.join(self.cmd_publish()) + ")")
        say("\n[step 4: drill]")
        say("  " + shlex.join(self.cmd_keys_list()) + "   # functionKeys.default, kept in memory")
        say(
            f"  POST https://{host}/budget/clear  headers: x-functions-key: {mask}, "
            f"X-Wattracker-Budget-Token: {mask}"
        )
        say("  " + shlex.join(self.cmd_entity_show(storage)))

    def execute(self) -> int:
        started = self.now()
        finding: str | None = None
        try:
            self.local_preflight()
        except MigrationError as exc:
            if not self.o.dry_run:
                self.fail(0, str(exc))
                return 1
            finding = str(exc)
        if self.o.dry_run:
            self.print_plan()
            if finding is not None:
                print(
                    f"\ndry-run finding: a real run would stop at preflight: {finding}",
                    file=sys.stderr,
                )
                return 1
            self.say(
                "\ndry-run complete; local checks passed. Run again without --dry-run to migrate."
            )
            return 0
        order = [0, *range(self.o.from_step, 5)]
        for step in order:
            step_started = self.now()
            self.say(f"\n== step {step}: {STEP_NAMES[step]} ==")
            try:
                if step == 0:
                    self.step0_preflight()
                else:
                    getattr(self, self.STEPS[step])()
            except MigrationError as exc:
                self.records.append(StepRecord(step, "FAIL", _utc(step_started), _utc(self.now())))
                self.fail(step, str(exc))
                self.print_summary(started)
                return 1
            except KeyboardInterrupt:
                self.records.append(StepRecord(step, "INTERRUPTED", _utc(step_started), _utc(self.now())))
                self.fail(step, "interrupted")
                return 130
            except Exception as exc:  # never echo str(exc): it could carry output
                self.records.append(StepRecord(step, "FAIL", _utc(step_started), _utc(self.now())))
                self.fail(step, f"unexpected {type(exc).__name__}")
                self.print_summary(started)
                return 1
            self.records.append(StepRecord(step, "PASS", _utc(step_started), _utc(self.now())))
        self.print_summary(started)
        return 0

    def fail(self, step: int, message: str) -> None:
        resume = self.o.from_step if step == 0 else step
        print(f"\nerror: step {step} ({STEP_NAMES[step]}) failed: {message}", file=sys.stderr)
        print(
            f"Fix the cause, then resume with --from-step {resume}:\n  "
            + self.o.resume_command(resume),
            file=sys.stderr,
        )

    def print_summary(self, started: float) -> None:
        lines = [
            "",
            "----- paste into #339 -----",
            "#### Flex migration run (`scripts/migrate_budget_hook.py`)",
            f"- started {_utc(started)}, finished {_utc(self.now())}",
        ]
        skipped = [s for s in range(1, self.o.from_step)]
        lines.append("")
        lines.append("| step | result | started | finished |")
        lines.append("|---|---|---|---|")
        for step in skipped:
            lines.append(f"| {step}. {STEP_NAMES[step]} | skipped (--from-step) | | |")
        for record in self.records:
            lines.append(
                f"| {record.step}. {STEP_NAMES[record.step]} | {record.result} | "
                f"{record.started} | {record.finished} |"
            )
        lines.append("")
        lines.append(f"- principalId: `{self.facts.principal_id or 'n/a'}`")
        lines.append(f"- defaultHostName: `{self.facts.host or 'n/a'}`")
        lines.append(f"- subnet: `{self.facts.subnet_id or 'n/a'}`")
        if self.drill_status is not None:
            lines.append(f"- drill: POST /budget/clear -> HTTP {self.drill_status}")
        if self.facts.row_updated_at:
            lines.append(
                "- CloudControl kill-switch row: writes_enabled=true, public_enabled=true, "
                f"reason=\"{OPERATOR_CLEAR_REASON}\", updated_at {self.facts.row_updated_at}"
            )
        lines.append("----- end -----")
        self.say("\n".join(lines))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # argparse messages echo argv values
        self.print_usage(sys.stderr)
        raise MigrationError("invalid command-line arguments")


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--resource-group", required=True)
    parser.add_argument("--subscription", required=True, help="subscription ID (GUID)")
    parser.add_argument("--location", default="eastus2")
    parser.add_argument("--function-app-name", required=True)
    parser.add_argument("--bootstrap-storage-name", required=True)
    parser.add_argument("--params", required=True, type=Path,
                        help="infra/azure/main.local.bicepparam (untracked)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--from-step", type=int, default=1, choices=(1, 2, 3, 4))
    parser.add_argument("--confirm-delete", metavar="NAME",
                        help="non-interactive confirmation for deleting the non-Flex app")
    return parser


def main(argv: Sequence[str] | None = None, **migrator_kwargs: Any) -> int:
    try:
        args = build_parser().parse_args(argv)
    except MigrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    options = Options(
        resource_group=args.resource_group,
        subscription=args.subscription,
        location=args.location,
        function_app_name=args.function_app_name,
        bootstrap_storage_name=args.bootstrap_storage_name,
        params=args.params,
        dry_run=args.dry_run,
        from_step=args.from_step,
        confirm_delete=args.confirm_delete,
    )
    return Migrator(options, **migrator_kwargs).execute()


if __name__ == "__main__":
    raise SystemExit(main())
