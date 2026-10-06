"""``python -m eios_policy.lock <policy.yaml>``: pin the digest of a reviewed Root Policy file."""

from eios_policy.root_policy import write_lock

if __name__ == "__main__":  # pragma: no cover
    import sys
    from pathlib import Path

    print(write_lock(Path(sys.argv[1])))
