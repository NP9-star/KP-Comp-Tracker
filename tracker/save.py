"""Commit and push new data (done in Python so it behaves the same on Windows and Linux)."""
import subprocess
import sys
from datetime import datetime, timezone


def git(*args, check=True):
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        print(r.stdout, r.stderr, file=sys.stderr)
        raise SystemExit(f"git {' '.join(args)} failed")
    return r


def main():
    git("config", "user.name", "tracker-bot")
    git("config", "user.email", "tracker-bot@users.noreply.github.com")
    git("add", "data", "docs/data")
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        print("No changes to save.")
        return
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    git("commit", "-q", "-m", f"Update availability {stamp}")
    import time
    for attempt in range(5):
        r = git("pull", "--rebase", "--autostash", "-q", check=False)
        if r.returncode != 0:
            print("pull failed:", r.stdout, r.stderr, file=sys.stderr)
            git("rebase", "--abort", check=False)
        p = git("push", "-q", check=False)
        if p.returncode == 0:
            print("Saved.")
            return
        print(f"push attempt {attempt + 1} failed:", p.stderr.strip()[:300], file=sys.stderr)
        time.sleep(10 * (attempt + 1))
    raise SystemExit("Could not push results")


if __name__ == "__main__":
    main()
