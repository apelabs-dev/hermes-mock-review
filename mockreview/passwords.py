"""Local per-review passwords; never included in browser state or published files.

password_words.txt contains EFF's 7,776-word large list (dice indices removed):
https://www.eff.org/files/2016/07/18/eff_large_wordlist.txt
Copyright Electronic Frontier Foundation, CC BY 3.0 US:
https://creativecommons.org/licenses/by/3.0/us/
Four independent uniform choices provide approximately 51.7 bits of entropy.
"""

import secrets
from pathlib import Path


def generate() -> str:
    words = Path(__file__).with_name("password_words.txt").read_text(encoding="utf-8").splitlines()
    return " ".join(secrets.choice(words) for _ in range(4))
