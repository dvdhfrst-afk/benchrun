# Web interface and HTTP API

`benchrun serve` shows a results database in a web browser and answers the same questions as JSON
for scripts. Pages are rendered on the server, with one stylesheet, no scripts and nothing loaded
from anywhere else, so they work on a bench PC with no internet connection. Pages and the API only
read the database. With `--allow-runs`, the browser can also start a run, one at a time, from a
folder of plans.

Everything below was run on simulator data.

## Starting the server

```bash
python -m benchrun serve
```

```text
benchrun web interface for C:\bench\benchrun.sqlite
  http://127.0.0.1:8765/
Press Ctrl+C to stop.
```

Open the address in a browser. Ctrl+C stops the server; so does a service stop (SIGTERM, or
Ctrl+Break on Windows). Each request is logged on standard error as one line; `--quiet` leaves
that out.

| Option | Meaning |
|---|---|
| `--db FILE` | results database (default `benchrun.sqlite`). It must exist unless `--allow-runs` is given. An older schema is upgraded once at start, the way every command does. |
| `--host ADDRESS` | address to listen on (default `127.0.0.1`, this computer only) |
| `--port N` | port (default 8765; 0 picks any free port and prints it) |
| `--public` | needed for a `--host` that other computers can reach |
| `--allow-runs` | let the browser start runs; needs `--plans-dir` |
| `--plans-dir DIR` | the folder of `.json` plans the browser may choose from |
| `--quiet` | no line per request |

There is no login. Without `--public`, a host that is not this computer only is refused:

```text
Cannot continue:
  --host 0.0.0.0 can be reached from other computers, and there is no login; add --public if that is what you want
```

With `--public` the server starts and prints a warning that anyone who can reach the computer sees
every result, and, with `--allow-runs`, can start runs on the bench.

```bash
python -m benchrun serve --db results.sqlite --port 0 --quiet
```

## Pages

Times are UTC everywhere, as the database stores them.

| Page | Shows |
|---|---|
| `/` | Runs still marked running, yield today and this week by plan (the week starts Monday 00:00 UTC), and the newest runs |
| `/runs` | Every run, newest first, 50 to a page, filtered by serial, plan, product, station, lot, result and start dates; download links for exactly the runs listed |
| `/runs/ID` | One run: its steps, every measurement with its verdict, the sweep plots the HTML report draws, the instruments with their asset tags, links to every report format, to its JSON, to the unit's history, to the plan's statistics and to its lot |
| `/serials/SERIAL` | Every run of one unit, with its latest result |
| `/plans` | Every plan that has a run: versions, runs, units, first-pass and final yield, first and last run |
| `/stats?plan=NAME` | The statistics `stats --format html` gives: runs used and left out, yield, the failure Pareto, a summary per measurement, and a histogram and an individuals control chart for each. The form narrows by product, station, lot, serial prefix, dates and measurement. |
| `/lots`, `/lots/ID` | Lots with quantity, units tested, units still to test, first-pass and final yield; a lot's page adds the failures by measurement, its statistics and its runs |
| `/stations` | Each station and the instrument in each role |
| `/instruments`, `/instruments/TAG` | The inventory, filtered by driver, status or text; one instrument's stations and the runs it took part in |
| `/products`, `/products/PART` | Part numbers, with their variables and limits |
| `/audit` | The audit log, newest first, filtered by subject, action, actor and dates |

Yield is the statistics engine's: each serial counts once, from its complete runs, first-pass from
its first and final from its last; aborted, unfinished and some-steps runs are counted apart. A
lot's units tested are the serials in it with a complete run, and units still to test are its
quantity less those.

A serial, asset tag or part number holding `/` or `\` is linked with a query instead of a path,
such as `/serials?serial=SN%2F1004`, because the server refuses encoded slashes in a path.

## Downloads

| Address | Gives |
|---|---|
| `/runs/ID/report/FORMAT` | one run's report in any format of `report --format`: html, csv, junit, json, markdown, certificate |
| `/download/export?format=csv` (or `jsonl`, `xlsx`) | what `export` writes, for the runs the filters choose |
| `/download/archive` | what `archive export` writes, for the runs the filters choose; `archive verify` and `archive import` accept it |

The download filters are the run list's: `serial`, `plan`, `product`, `station`, `lot` (name or
id), `status`, `since` and `until`. The run list's download links carry the filters it shows. A
filter that matches nothing gives a 404 and no file. Downloads come as attachments; an HTML report
opened from one is sandboxed and can run nothing.

## The JSON API

Every answer is one JSON object, and every list that can be long is paged with `page` (from 1) and
`per_page` (1 to 500, default 50).

| Endpoint | Answer |
|---|---|
| `/api` | the list of endpoints |
| `/api/runs` | `runs`, `filter` and `paging`; takes the run list's filters |
| `/api/runs/ID` | `run`: everything `show --json` gives, and `lot`, the lot's name |
| `/api/serials/SERIAL` | `serial`, `runs` and `paging` (or `/api/serials?serial=...`) |
| `/api/plans` | `plans` |
| `/api/stats?plan=NAME` | what `stats --json` gives; `view` is `summary` (default), `yield`, `pareto`, `histogram` or `control`, the last two with `measurement=NAME`, and `bins` with `histogram`. It takes `product`, `station`, `lot`, `serial` (a prefix), `since`, `until`, `step`, `measurement` and `limit_runs`. |
| `/api/lots`, `/api/lots/ID` | `lots`; `lot` with its `yield` per plan and `pareto` |
| `/api/stations` | `stations`, each with its `roles` |
| `/api/instruments`, `/api/instruments/TAG` | `instruments` (filters `driver`, `status`, `search`); `instrument` with `stations` and `recent_runs` |
| `/api/products`, `/api/products/PART` | `products`; `product` with `variables` and `limits` |
| `/api/audit` | `entries` and `paging`; filters `subject` (ending in `:` for all of a kind), `action`, `actor`, `since`, `until` |

`paging` always has `page`, `per_page` and `has_more`; the run lists also have `total` and
`pages`. For example `/api/runs?per_page=2` answered:

```text
"paging": {
  "page": 1,
  "per_page": 2,
  "has_more": true,
  "total": 6,
  "pages": 3
}
```

A number that is not finite, such as an overloaded reading, is `null`, as in the JSON report, so
any JSON reader can read every answer. An error is an object with the HTTP status:

```text
{
  "error": {
    "status": 404,
    "code": "not_found",
    "message": "no run with id 999"
  }
}
```

| Status | When |
|---|---|
| 400 | a filter, page number or request target is not acceptable; the message says which |
| 403 | a form without this server's token or from another site, or runs are switched off |
| 404 | the run, lot, unit or page does not exist |
| 405 | the method is not used at that address (the `Allow` header says which are) |
| 409 | a run started from the browser is still going |
| 411, 413, 415 | a form without a length, over 16 KiB, or not URL-encoded |
| 414 | a request target over 4096 characters |
| 421 | the Host header names another computer (see Security) |
| 503 | the results database cannot be read |

## Starting runs from the browser

```bash
python -m benchrun serve --allow-runs --plans-dir examples
```

The menu then has **Start a run**. Its form takes:

- a plan, chosen from the `.json` files directly in `--plans-dir` (never a subfolder, a link or a
  path typed in), or none, to use the chosen product's own plan;
- the serial number, and optionally an operator label;
- optionally a product and a station, chosen from those in the database;
- whether to use private simulators (`--sim`), which cannot be combined with a station.

The server checks all of it, then starts `python -m benchrun run` as a child process with an
argument list, never through a shell, writing to the same database. Its output appears as it
arrives, and the page refreshes every 2 seconds until the run ends; then it shows the result, the
exit code and a link to the run. One run at a time: while one is going, the form is not shown and
another start gets a 409.

When the plan asks the operator something, the page shows the question and a **Done, go on**
button, which presses Enter for the run. **Stop the run** sends the stop request a terminal does
(Ctrl+Break on Windows, Ctrl+C elsewhere): the run switches its outputs off and records itself as
aborted, with a reason saying it was interrupted. Stopping the server stops a run still
going the same way and waits up to 60 seconds for it to finish. On Windows the stop reaches the run
through the console the server runs in; if it cannot be sent, the page says so, and the run must be
stopped at the bench.

A run from `fixture_with_operator.json` on simulators showed, while it waited and after **Done, go
on**:

```text
Run 6: serial SN-2001, plan fixture-with-operator 1.0
  supply     127.0.0.1:54148          benchrun,SIM-PSU,SIM0002,1.0
  dmm        127.0.0.1:54147          benchrun,SIM-DMM,SIM0001,1.0
OPERATOR: Seat the unit in the fixture and close the lid
Press Enter to go on, or Ctrl+C to stop the run.
[PASS] no_short (1 measured, 0 failed)
[PASS] powered (1 measured, 0 failed)
RESULT: PASS
```

## Security

- It listens on 127.0.0.1 unless told otherwise, and then only with `--public`.
- Without `--public`, it answers only requests whose Host header is `localhost`, `127.0.0.1` or
  `[::1]` with its own port, so a web page elsewhere cannot point a name of its own at it.
- Pages and the API open the database read-only, a new connection for each request.
- Everything shown is escaped. Every answer carries a Content-Security-Policy that allows no
  scripts, no frames (`frame-ancestors 'none'`) and nothing from elsewhere, with
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY` and `Cache-Control: no-store`.
- Request targets with `..`, `.`, a backslash, encoded slashes or backslashes, a NUL or invalid
  UTF-8 are refused, as are absolute URLs; repeated query fields and more than 64 fields are
  refused too.
- Everything that changes something (start, stop, go on) is a POST of at most 16 KiB that must
  carry a token made fresh each time the server starts, and an Origin (or Referer) of the server
  itself.
- Nothing from a request is put into a header: a report's file name is made from the run's id, its
  serial with every unusual character replaced, and its start time; an export's or archive's from
  the time of the download.
