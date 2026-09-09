# Validation

Verified locally on 7 September 2026 using Windows and Python 3.12.14.

| Check | Result |
| --- | --- |
| Full unittest discovery | 285 tests passed in 546.614 seconds |
| Offline CLI demo | Three fictional jobs imported, repeat imports rejected, three packets generated |
| Interactive demo | Loaded, generated a packet, switched roles without an exception |
| Dependency consistency | `pip check` reported no broken requirements |
| Staged-file review | No excluded runtime files or known private identity/credential patterns detected |
| Git whitespace check | Passed |

The dependency set is recorded in `requirements-lock.txt`. The suite includes mocked browser integrations; it does not prove that live Gmail or SEEK integrations currently work. No real application was submitted as part of this verification. Pattern checks are a limited review, not a guarantee that every possible secret can be detected.

GitHub Actions has been configured but has not run remotely yet.
