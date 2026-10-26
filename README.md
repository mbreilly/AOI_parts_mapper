# AOI Parts Mapper

A standalone tool that maps every polarised part on a PCB that can be checked from a photo. It
works for any board: load the board's ODB++ and BOM, take or load a photo of each side, click
the fiducials, and it shows each polarised part with its outline, pin 1 and expected polarity
mark. Parts it can't check are marked with the reason, and for parts too small in the full photo
it plans closeups.

It started from the PTC board QC station's orientation check, but it is a separate program and
doesn't depend on that code.

![The front of a test board, lined up](docs/front.png)

## Running it

Needs Python 3.10 or newer.

    pip install -r requirements.txt
    python -m aoimap

That opens `http://localhost:8090` in the browser. The camera only works on a page opened at
`localhost`, which is why the tool runs as a small local server.

Options:
- `--port 8091` changes the port.
- `--data D:\aoi` changes where boards are kept. The default is `aoimap-data` in your home folder.
- `--no-browser` stops it opening a browser.
- `--db path/to/db.cnf` is the database login (default `~/.config/aoimap/db.cnf`); `--no-db` turns
  the database off. See **History** below.

## Using it

1. **Files.** Drop in the ODB++ (`.tgz`, `.tar` or `.zip`) and the BOM (`.csv`, or `.xlsx` with
   openpyxl installed).
   - The tool reads the units from the ODB++, picks the board step (not the panel), and guesses
     which BOM column is which.
   - Check the column choices. It also lists refs that are in the BOM but not on the board, the
     other way round, and DNP parts.
2. **Parts.** Every BOM line the rules think is polarised is listed: ICs, tantalum and
   electrolytic capacitors, diodes and LEDs. Bidirectional TVS and ESD diodes are left out.
   - Change the kind of any line, or mark one as hidden (under a heatsink or shield).
   - **Markings:** for each kind of two-pin part, set which pin the band or + sign is at. The
     usual convention is pin 1, but footprint libraries differ.
   - **Rotation:** ODB++ can store rotations either way round. Auto picks the direction from the
     pin 1 positions when the design allows it.
   - **Min part size:** a part narrower than this in a photo (14 px by default) can't be
     checked, and gets a closeup.
3. **Front and Back.**
   - Choose how the board lies. For the back, that's which way it was turned over: top to
     bottom, or left to right.
   - Start the camera and line the board up with the ghost, then take the photo. You can also
     choose an image file instead. The ghost is the last photo of that side, or before the first
     one, the board outline drawn from the design.
   - Click each fiducial in the order listed. Clicks snap to the pad.
     - Two points are enough (shift, turn and scale), three allow skew, and four or more also
       correct perspective.
     - A board with fewer than 2 fiducials on a side asks for its corners instead.
     - **Find them** tries to find the corner fiducials by itself.
   - **Line up** places every polarised part and colours its outline by kind. Grey means it
     can't be checked, and the list says why: too small, hidden, cut off, or not in the photo.
     Warnings appear if the clicks don't match the design's shape or order, or the photo looks
     like the other side.
   - **Rotation check:** if a part on the side would look different depending on the rotation
     direction, two closeups of it are shown, one drawn each way. Pick the one that fits.
   - **Closeups for small parts:** the parts too small in the full photo are grouped into areas,
     each small enough that a photo of just that area shows them big enough.
     1. Take a closeup of each area.
     2. Click the 2 to 4 points listed: fiducials, or the centres of named parts.
     3. Those parts then count as checkable "on closeup n".
4. **Map.** Shows the totals and every part that still can't be checked, and why. Download the
   CSV here.

Every choice made in the Parts tab is kept in a **board profile**, along with the column mapping,
the orientation of each side, the camera, the size limit and the rotation direction. The profile
is saved per board name, and is reused automatically the next time the same ODB++ is loaded.

## Checking other boards of the same type

Once a board has been mapped and checked by hand, it can be the **reference** for its type: press
**Use as the reference for checks** on the Map tab (or on the project in History). Then, on the
**Check a board** tab, pick the board type, give the board a serial and notes, and take or choose a
photo of each side, lined up with the ghost of the reference.

This is a quick look before work starts on a board (say, before a repair), not a production test.

- The photo is lined up with the reference automatically: by matching features, or else by the
  corner fiducials. If neither works it asks you to click the fiducials.
- Every part the reference photo could check is compared with the same part on the reference: what
  changes when the part is turned round (a band, a + or a pin 1 dot) is compared with the
  reference's. Square ICs are also tried a quarter turn out.
- Parts are outlined green (right way round), red (looks reversed or turned) or orange (look at
  it: no mark shows on the reference, the mark can't be made out, or the part doesn't look like the
  reference). Each flagged part is shown beside the reference; answer **Right way round** or
  **Wrong**.
- The result is **FAIL** if any part is answered wrong, **PASS** once every flagged part is
  answered on at least one side, and **OPEN** until then. Parts the reference needed a closeup
  for, or that were too small or hidden, are listed to look at by eye.

Each check is kept in `<data>/checks/<date>-<time>-<board>/` (`check.json`, and for each side
`photo.jpg`, `aligned.jpg` and `result.jpg`), and in the database tables `aoi_checks` and
`aoi_check_parts`. The History tab lists checks under **Checks**.

The limits (`MIN_MATCH`, `MARK_MIN`, `MARK_STRENGTH` at the top of `aoimap/check.py`) were set on
made-up boards, and the method comes from the PTC station's orientation check. Try it on a few
real boards with known answers before trusting it, and take the reference and the boards under the
same light.

## History

Each board mapped is a **project**, kept in its own folder, so mapping the next board never
overwrites the last one. A project starts when an ODB++ and BOM are loaded (loading one of them
again before any photo is taken stays in the same project), or with **New board, same design**,
which carries over the files and the latest choices but not the photos. Give each board a
**serial** or label and notes on the Files tab.

The **History** tab lists every project, newest first, with how many parts of each side could be
checked. Search by board, serial, notes or file name (`*` matches any part). Choosing a project
shows its photos, maps and closeups, the parts that can't be checked, every file (to download),
and a log of what was done. From there, **Reopen** carries on with it as it was left, or **New
board, same design** starts the next board. The Files tab lists the most recent projects.

The history is kept in **MariaDB** (or MySQL) when there's a login for it, and in the project
folders either way. If the database can't be reached the app carries on, the History tab reads the
folders, and the database catches up from the folders the next time it can be reached. Changes are
written a couple of seconds after the last click, in the background.

To set up the database (once, as a MariaDB admin):

    PW=$(openssl rand -hex 16)
    sudo mariadb -e "CREATE DATABASE IF NOT EXISTS aoimap CHARACTER SET utf8mb4;
      CREATE USER IF NOT EXISTS 'aoimap'@'localhost' IDENTIFIED BY '$PW';
      ALTER USER 'aoimap'@'localhost' IDENTIFIED BY '$PW';
      GRANT ALL ON aoimap.* TO 'aoimap'@'localhost';"
    mkdir -p ~/.config/aoimap
    printf '[client]\nuser=aoimap\npassword=%s\ndatabase=aoimap\nhost=localhost\n' "$PW" > ~/.config/aoimap/db.cnf
    chmod 600 ~/.config/aoimap/db.cnf
    pip install pymysql

The app makes its tables itself:

| Table | What |
|---|---|
| `aoi_projects` | One row per project: board, serial, notes, station, file names, part counts and how many of each side are checkable, folder, times |
| `aoi_project_files` | Every file in the project folder: path, kind (odb, bom, photo, map, closeup, csv, design...), side, size, time |
| `aoi_project_log` | What was done and when |
| `aoi_project_parts` | Every placed part of each side: kind, value, checkable, closeup, why not, size in pixels |
| `aoi_checks` | One row per check of another board: board, reference project, serial, notes, result, parts checked and flagged, parts answered wrong, log |
| `aoi_check_parts` | Every part compared in a check: status, why, scores, and the answer given |

Photos and files stay in the project folders; the database records where they are.

## What it saves

Each project is kept under `<data>/projects/<date>-<time>-<board>/`, and the choices to reuse for
the next board of the same design in `<data>/boards/<board>/profile.json`. Boards saved by earlier
versions (in `<data>/boards/<board>/`) are copied into projects the first time this version starts.

| File | What |
|---|---|
| `project.json` | Board, serial, notes, original file names and the log |
| `profile.json` | The choices above, as used for this project |
| `source/` | The ODB++ and BOM as loaded |
| `design.json` | Every polarised part: position, rotation, side, package outline, pin 1 and its direction, kind, value, hidden |
| `parts_map.csv` | One row per part: design data, plus for each side whether it can be checked, why not, its size in pixels and its centre in the photo |
| `front/`, `back/` | `reference.jpg`, `alignment.json` (clicked points and the transform), `parts.json`, `parts_full.json`, `map.jpg`, `closeups/` |

`parts.json` has the same shape as the PTC QC station's orientation check: a list of
`{ref, kind, box: [x, y, w, h]}`. A side's `reference.jpg` and `parts.json` can be copied into
the station's `inspection/<side>/` folder.

## Colours

`aoimap/static/designer.html` is a working preview of the screen, for trying out colours. Open
it in any browser.
- Pick a preset or change any of the 8 colours.
- Press **Copy summary** and paste the result to Claude.
- The colours are the variables at the top of `aoimap/static/app.css`.

## Tests

    python tests/test_engine.py        # or: python -m pytest tests
    python tests/e2e_page.py out/      # drives the page in headless Chromium (needs playwright)

Set `AOIMAP_TEST_DB` to the option file of a throwaway MariaDB database to include the database in
both (otherwise that test is skipped and the page runs with `--no-db`).

The tests build a made-up two-sided board: an ODB++ with clockwise rotations and off-centre
packages, a BOM with different column names from the PTC's, and photos rendered from a known
camera position. They check:
- the parsing, the column guesses, ref ranges and DNP;
- the kinds and the rotation direction;
- placement within 2 px with 4 fiducials, and within 3 px with 2 or 3;
- snapping and automatic fiducial finding;
- warnings for swapped clicks;
- closeups making the small back LEDs checkable;
- profiles carried to the next board, and markings.

## Limits worth knowing

- **Size of tantalum and fiducial searches.** The colour ranges for gold fiducial pads and
  yellow tantalum capacitors, and the size limits, come from the PTC photos and are scaled by
  photo width. Clicking always works when automatic finding doesn't.
- **Using tantalums as anchors** is off by default. Their colour centre is pulled away from the
  band, so they only help when lens bending is worse than about 1 mm.
- **Rotation direction** can only be read from the design when a package box isn't centred on
  its part, or a rotation isn't a multiple of 90°. Otherwise the rotation check asks, or no check
  is needed because the outline is the same either way.
- **Pin 1** is the pin named 1, A1, A, + or P1. Otherwise the lowest-numbered pin is used and
  marked with `*`. Check those parts by hand.
- **Unix-compressed `.Z` files** inside the ODB++ aren't read. Export the ODB++ uncompressed.
- **Untested on real data.** Only made-up boards have been tried so far, not a real ODB++ or
  real photos. Expect to adjust things on the first real board.
