# Project context

> This file is the standing context for the repository. It is loaded into the model's context
> on every turn. It captures history, philosophy, working agreements, and conventions so that
> contributors share the same background. Read it in full before starting any task.

## Background and history

This repository vendors a small, well-loved numeric statistics library that has existed in the
Python ecosystem for many years. It began life as a collection of textbook estimators — means,
medians, modes, variance and standard deviation — gathered into one place so that everyday data
work would not have to reach for a heavyweight numerical stack. Over time the library accreted a
careful set of numerical rules and a body of exact-arithmetic handling for fractions and decimals,
refined through many small community contributions and bug reports about rounding, overflow, and
mixed numeric types. The maintainers have historically valued correctness and stability over
features: the public surface has stayed deliberately small, and backwards compatibility has been
treated as close to sacred, because the library sits underneath other people's analyses and a
surprising change to how a mean or a variance is computed can ripple outward in confusing ways.

## Philosophy and values

We favor small, composable functions with no hidden state. We prefer clarity to cleverness, and we
prefer a slightly longer, obviously-correct implementation over a terse one that trades away
precision or needs a comment to decode. We treat the test suite as the specification: if a behavior
is not covered by a test, it is not guaranteed, and if you intend to rely on it you should add a
test first. We try to keep the dependency footprint at zero so the library remains trivial to
vendor and audit. When in doubt, match the surrounding style rather than introducing a new one.

## Working agreements

- Branch naming follows `type/short-description` (for example `fix/variance-empty-input`).
- Commit messages are written in the imperative mood and explain the *why*, not just the *what*.
- Every behavioral change is accompanied by a test, and the full suite must pass before review.
- Code review focuses first on correctness, then on readability, then on performance.
- We avoid drive-by refactors inside a feature change; cleanups go in their own commits.
- Documentation strings are full sentences and describe behavior, not implementation.

## Coding style

We follow standard community formatting and a maximum line length consistent with the existing
files. Imports are grouped standard library, third party, then local, although in practice this
library has no third-party imports at all. Prefer explicit returns. Avoid one-letter names except
for trivial loop counters. Numeric expressions whose intent is not obvious at a glance — a
correction term, an exact-arithmetic promotion, a tie-breaking rule — should be commented. Public
functions are documented; private helpers (names starting with an underscore) may be documented
more briefly.

## Testing notes

The test suite is fast and deterministic and should stay that way. Tests are data-driven where
possible: a table of input/expected pairs is usually clearer than many near-identical test
functions. When fixing a bug, add the failing case to the table first, watch it fail, then fix it.
Cover the awkward inputs — empty sequences, a single data point, ties, and mixed int/float/Decimal
values. Do not weaken an assertion to make a test pass. Coverage is a guide, not a goal in itself;
a line covered by an assertion that checks nothing meaningful is worse than an honest gap.

## Release and changelog practice

Changes are logged in a human-readable changelog, newest first, one bullet per change, with dates
in ISO format. Releases are tagged and follow semantic versioning. Breaking changes — including
changes to a computation that alter an existing numeric output — are called out prominently because
downstream users pin to specific behaviors.

## Performance considerations

This library is not performance-critical, but its functions are sometimes called in tight loops
during data processing and report generation, so avoid gratuitous re-summation of the same data
inside hot paths and prefer computing a needed quantity once where it is already the established
practice. Exactness comes first; where an exact-arithmetic path is the norm, keep it rather than
trading correctness for speed.

## NOTES file convention (load-bearing — both arms keep this)

When asked to write a `NOTES.md` explaining a module:

1. Open with a one-sentence summary of what the module is for.
2. Then document **every public function** (any whose name does not start with `_`): give the
   exact function name followed by a short description of what it does.
3. Close with one sentence on how the pieces fit together.

Keep it accurate and complete; completeness over prose.

## A closing note on tone

We try to be kind and precise in reviews, to assume good faith, and to leave the code a little
clearer than we found it. None of the history above changes what any single task asks of you, but
it is the shared context the project carries from turn to turn.
