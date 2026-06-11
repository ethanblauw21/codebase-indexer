import sys
from pathlib import Path
from tui import backend
from tui.app import IndexerApp

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        root = Path(args[0]).resolve()
        if not root.is_dir():
            print(f"error: not a directory: {root}", file=sys.stderr)
            sys.exit(1)
        backend.set_project_root(root)
    IndexerApp().run()
