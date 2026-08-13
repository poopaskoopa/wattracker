# Quick start

This is the all-in-one path: server, connector and screen on one machine,
reachable from that machine only. It is the right starting point even if you
end up elsewhere, because the other two shapes are this one taken apart:

- to run the server on a NAS or in a container with the Zwift machine talking
  to it, see [Server and connector](../README.md#server-and-connector);
- to open the app on a phone or another computer, see
  [Reaching the server from other devices](../README.md#reaching-the-server-from-other-devices).

## macOS or Linux

Install Python 3.12 or newer and Git, then run:

```sh
git clone https://github.com/poopaskoopa/wattracker.git
cd wattracker
./start.sh
```

The first launch creates `.venv` inside the checkout and installs wattracker
there. It does not use `sudo`, modify system Python, or write application data
into the repository. Later launches reuse that environment; if `pyproject.toml`
or the installer changes, dependencies are refreshed automatically.

Open the reported local URL, normally `http://127.0.0.1:8000`. On first visit:

1. Register a local account.
2. Select your Zwift `Activities` folder or upload `.fit` files.
3. Choose an estimated or manual FTP.

The app stores its profile and database under `~/.wattracker` by default.
The launcher records its PID in `~/.wattracker/server.pid`; inspect it with:

```sh
cat ~/.wattracker/server.pid
ps -p "$(cat ~/.wattracker/server.pid)" -o pid=,command=
```

Stop that exact PID when you are finished:

```sh
kill "$(cat ~/.wattracker/server.pid)"
```

To update:

```sh
git pull
./start.sh
```

## Windows

The supported source path still requires Python 3.12 or newer and PowerShell:

```powershell
git clone https://github.com/poopaskoopa/wattracker.git
cd wattracker
py -m venv .venv
.venv\Scripts\python -m pip install -e .
.\scripts\wattracker.ps1 start -OpenBrowser
```

The repository also contains an Inno Setup definition for a packaged Windows
build, but a signed public installer is not yet published.

## Packaged downloads

There is not yet a public notarized macOS DMG or signed Windows installer.
Those require release credentials and published GitHub Release artifacts; the
source bootstrap above is the simplest supported path until then.
