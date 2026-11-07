# Continuous integration

benchrun needs Python 3.9 or newer and no other packages, so a job has little to set up. The tests use
ephemeral ports on 127.0.0.1 and need no instruments.

```bash
python -m unittest discover
```

To run a plan on the simulators as a check of the plan itself, and keep the JUnit XML file for your
system's test report page:

```bash
python -m benchrun run examples/regulator_quick.json --serial CI-1 --sim --db ci.sqlite --report-dir reports
```

Exit codes tell a job what happened: 0 pass, 1 the unit failed, 2 a bad plan or bad arguments, 3 aborted.

A job definition to copy, in the form GitHub Actions takes. It is not active in this repository, and it
has not been run on that service:

```yaml
name: tests
on: [push, pull_request]
jobs:
  tests:
    strategy:
      matrix:
        os: [ubuntu-latest, windows-latest]
        python: ["3.9", "3.13"]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python }}
      - run: python -m unittest discover
```

Plans on a real bench should use `--auto-confirm` only if nothing in them truly needs a person there.
Without it, a prompt with no one to answer aborts the run, which is usually what a job wants.
