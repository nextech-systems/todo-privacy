"""Build a regular (non-editable) wheel and check it contains everything the app needs.

    ./env/bin/python apps/todo_privacy/todo_privacy/tests/check_wheel.py apps/todo_privacy

Exits non-zero if a required file is missing.
"""

import os
import subprocess
import sys
import tempfile
import zipfile

REQUIRED_SUFFIXES = (".py", ".txt", ".json")


def main(app_dir):
	app_dir = os.path.abspath(app_dir)
	package = os.path.join(app_dir, "todo_privacy")
	expected = set()
	for root, _dirs, files in os.walk(package):
		if "__pycache__" in root:
			continue
		for name in files:
			if name.endswith(REQUIRED_SUFFIXES):
				expected.add(os.path.relpath(os.path.join(root, name), app_dir).replace(os.sep, "/"))

	with tempfile.TemporaryDirectory() as out:
		subprocess.run(
			[sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "-q", "-w", out, app_dir],
			check=True,
		)
		wheel = next(os.path.join(out, f) for f in os.listdir(out) if f.endswith(".whl"))
		inside = set(zipfile.ZipFile(wheel).namelist())

	missing = sorted(expected - inside)
	for must in ("todo_privacy/modules.txt", "todo_privacy/hooks.py"):
		print(("ok      " if must in inside else "MISSING ") + must)
	print(f"{len(expected) - len(missing)} of {len(expected)} package files in the wheel")
	for path in missing:
		print("MISSING " + path)
	sys.exit(1 if missing else 0)


if __name__ == "__main__":
	main(sys.argv[1] if len(sys.argv) > 1 else ".")
