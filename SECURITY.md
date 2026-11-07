# Security

## Reporting a problem

If you find a security problem, please use the private vulnerability reporting of wherever you got this
repository, if it offers that, instead of a public issue. Say what you did, what happened, and which
version (`python -m benchrun --version`). There is no paid support and no promised response time.

## What benchrun protects against

A plan or station file might come from someone else, so benchrun treats them as untrusted data:

- **No code runs from a plan.** Expressions are parsed and only arithmetic and a short list of functions
  run; nothing reaches `eval`. Command templates can only use their own named fields, with no attribute
  access.
- **Size and time are limited.** Large sweeps, deep nesting, huge powers, long expressions and very
  large files are refused before they can fill memory or hang.
- **Reports are escaped.** Text in HTML and XML reports is escaped. CSV text cells a spreadsheet would
  treat as a formula get a leading apostrophe. Control characters are removed before printing to a
  terminal.
- **File names stay inside the report folder.** A serial number such as `../../x` cannot write elsewhere.
- **Commands cannot be smuggled in.** Line breaks and non-ASCII characters in commands are refused.
- **The simulators listen on 127.0.0.1.** `sim` has no option to change that.

## What it does not protect against

- **A plan can command your instruments.** That is its purpose. A hostile plan can set a supply to its
  maximum, or send any SCPI through `write`. No check in benchrun can tell a bad setting from a good one.
  **Read a plan from someone else before running it on a bench**, especially its `write` calls and
  `commands` section. `validate` checks that a plan is well formed, not that it is safe to run.
- **The network.** SCPI over raw TCP has no login and no encryption. Anyone who can reach an instrument
  can command it and can read the traffic, and a device answering at an instrument's address can send
  false readings. Use `expect_idn` to catch mistakes, but it is no defence against a deliberate attacker.
  Keep instruments on a network you trust. benchrun only opens outgoing connections, apart from `sim`.
- **The results database.** Anyone who can write to the file can change it. The plan hash shows which plan
  ran, but it is not a signature. Use file permissions and backups if the records matter.
- **Safety.** benchrun is not a safety system. See "What benchrun cannot do" in the README.

Plan text, including anything sensitive in it, is stored in the results database and printed in reports.
Keep secrets out of plans.
