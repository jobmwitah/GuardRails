#!/usr/bin/env python3
import subprocess
import sys

# Run the unit tests
result = subprocess.run(
    [sys.executable, '-m', 'unittest', 'discover', '-v'],
    cwd='C:/Users/Administrator/GuardRails',
    capture_output=True,
    text=True
)
print(result.stdout)
if result.stderr:
    print("STDERR:", result.stderr, file=sys.stderr)
sys.exit(result.returncode)