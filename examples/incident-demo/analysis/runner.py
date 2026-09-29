"""Trusted image entrypoint; stdout is a bounded result envelope."""

import base64
import json
import os
import stat
import subprocess
from pathlib import Path


def main() -> None:
    limit = int(os.environ["AMADEUS_OUTPUT_BYTES"])
    count = int(os.environ["AMADEUS_OUTPUT_FILES"])
    output = Path("/outputs")
    # Both streams share the output tmpfs hard size limit; no host output bind exists.
    with (output / ".stdout").open("wb") as stdout, (output / ".stderr").open("wb") as stderr:
        process = subprocess.run(
            ["python", "-I", "/execute.py"],
            cwd=output,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp"},
            check=False,
        )
    files: dict[str, str] = {}
    size = 0
    truncated = False
    for path in sorted(output.iterdir()):
        if path.name in {".stdout", ".stderr"}:
            continue
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or len(files) >= count:
            truncated = True
            continue
        size += metadata.st_size
        if size > limit:
            truncated = True
            break
        files[path.name] = base64.b64encode(path.read_bytes()).decode()
    stdout_bytes = (output / ".stdout").read_bytes()
    stderr_bytes = (output / ".stderr").read_bytes()
    truncated |= size + len(stdout_bytes) + len(stderr_bytes) >= limit
    print(
        json.dumps(
            {
                "exit_code": process.returncode,
                "outputs": files,
                "stdout": stdout_bytes[:limit].decode(errors="replace"),
                "stderr": stderr_bytes[:limit].decode(errors="replace"),
                "truncated": truncated,
            }
        )
    )


if __name__ == "__main__":
    main()
