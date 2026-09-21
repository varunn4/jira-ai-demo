"""Cross-platform script to install frontend dependencies and build the React SPA."""

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


def main():
    root_dir = Path(__file__).resolve().parent.parent
    frontend_dir = root_dir / "frontend"

    if not frontend_dir.is_dir():
        print(f"Error: Frontend directory not found at {frontend_dir}", file=sys.stderr)
        sys.exit(1)

    # Check for node and npm
    node_cmd = shutil.which("node")
    npm_cmd = shutil.which("npm") or shutil.which("npm.cmd")

    if not node_cmd or not npm_cmd:
        print(
            "Error: Node.js / npm not found in PATH. Please install Node.js (18+) to build the frontend.",
            file=sys.stderr,
        )
        sys.exit(1)

    is_windows = platform.system().lower() == "windows"

    print(f"Working Directory: {frontend_dir}")
    print("Step 1/2: Installing dependencies (`npm install --no-audit --no-fund`)...")

    # Install dependencies
    install_res = subprocess.run(
        ["npm", "install", "--no-audit", "--no-fund"],
        cwd=frontend_dir,
        shell=is_windows,
        check=False,
    )
    if install_res.returncode != 0:
        print("Error: npm install failed.", file=sys.stderr)
        sys.exit(install_res.returncode)

    print("\nStep 2/2: Building SPA (`npm run build`)...")
    build_res = subprocess.run(
        ["npm", "run", "build"],
        cwd=frontend_dir,
        shell=is_windows,
        check=False,
    )
    if build_res.returncode != 0:
        print("Error: npm run build failed.", file=sys.stderr)
        sys.exit(build_res.returncode)

    dist_index = frontend_dir / "dist" / "index.html"
    if dist_index.is_file():
        print(f"\n[SUCCESS] React SPA built successfully -> {frontend_dir / 'dist'}")
    else:
        print(
            f"\n[WARNING] Build finished but {dist_index} was not found.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
