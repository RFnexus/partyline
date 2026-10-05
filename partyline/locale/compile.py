import glob
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def compile_all():
    failures = 0
    for source in sorted(glob.glob(os.path.join(HERE, "*.po"))):
        language = os.path.splitext(os.path.basename(source))[0]
        target_dir = os.path.join(HERE, language, "LC_MESSAGES")
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(target_dir, "partyline.mo")
        if shutil.which("msgfmt"):
            result = subprocess.run(["msgfmt", "--check", "-o", target, source])
            ok = result.returncode == 0
        else:
            try:
                from babel.messages.mofile import write_mo
                from babel.messages.pofile import read_po
            except ImportError:
                print("neither msgfmt nor the babel package is available", file=sys.stderr)
                return 1
            with open(source, "rb") as handle:
                catalog = read_po(handle)
            with open(target, "wb") as handle:
                write_mo(handle, catalog)
            ok = True
        print(f"{language}: {'ok' if ok else 'FAILED'}")
        failures += 0 if ok else 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(compile_all())
