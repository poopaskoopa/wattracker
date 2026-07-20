"""PyInstaller entry point with offline restore dispatch."""
import sys

if len(sys.argv) > 1 and sys.argv[1] == "restore":
    from wattracker.restore_backup import main

    raise SystemExit(main(sys.argv[2:]))

from wattracker.__main__ import main

main()
