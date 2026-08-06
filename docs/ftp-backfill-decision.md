# Historical TSS backfill — the numbers for the decision (#62)

Nothing has been written to the live database. Every figure below comes from a
`.backup()` copy of `~/.wattracker/wattracker.db` (520,843,264 bytes,
17,231 activities, 20 users, 26 `ftp_history` rows) taken through a
`file:...?mode=ro` handle, and from a dry run of `wattracker-ftp-rescore`
against that copy. The copy's md5 was identical before and after the dry run
(`da474528f7c8c3e60f8ab5d56b1627f4`). **The repair has not been run.**

Reproduce with:

```sh
wattracker-ftp-rescore --db /path/to/copy.db --json report.json
```

## Read this first

**1. TSS goes UP, not down.** Of the 12,874 ordinary rows that would change,
**10,569 go up and 1,995 go down**; the median change is **+9.6 TSS**. #54 was
approved on the premise that batch-imported rides are inflated and a backfill
would bring them down. That is not what this backfill does to this database.
The reason is visible in the per-user basis clusters below: most users' rides
were scored against the 200 W import default, and the earliest FTP recorded for
most users is **184.9 W** — *below* 200, so rescoring inflates them.

**2. The rule being applied to 99% of the database is "back-apply the earliest
FTP ever recorded".** `ftp_history` begins 2026-07-02; activities begin
2021-02-21. **17,032 of 17,211** scored rows (99.0%) predate their user's first
`ftp_history` entry, so "the FTP effective on the activity date" resolves to
the earliest entry that user has, applied back across five years. This matches
the app's existing `ftp_as_of` convention, but on this database it is the rule
for essentially every row, not an edge case.

**3. The 184.9 W estimate is unvalidated.** 16 of 20 users have exactly one
`ftp_history` row, all reading `184.9, estimated`, all written 2026-07-19 to
2026-07-26. Whether that number is right is the open question in #62 and it is
not answered here. The backfill's output is only as good as that estimate,
because for 99% of rows it *is* the estimate.

## What the dry run reports

```
Rows examined 17211; would change 15476; unchanged 1735; skipped 20

Populations (delta statistics cover changed rows only):
  ordinary  rows  14588  changed  12874  (up 10569, down 1995)  TSS delta min/median/max -624.6/+9.6/+167.3
  suspect   rows    267  changed    267  (up 0, down 267)       TSS delta min/median/max -1337.9/-726.4/-56.4
  corrupt   rows   2335  changed   2335  (up 0, down 2335)      TSS delta min/median/max -16136143.0/-44248.8/-0.1
  unscored  rows     21  changed      0  (up 0, down 0)         TSS delta min/median/max -/-/-

Summed CTL/ATL/TSB shift across users (corrupt and suspect rows excluded from
both sides): -35.17/-7.58/-27.58
```

Populations, and why they are split this way:

* **corrupt (2,335 rows)** — implied scoring basis `np / if_` below 50 W: the
  #60 damage. The selector is the implied basis, not `tss > 1000`, which finds
  only 2,199 of them because a short ride scored against 0.6 W still lands
  under 1,000 TSS. 50 W is not an invented number: it is the app's own
  `FTP_PLAUSIBLE_MIN_WATTS`, so "corrupt" means exactly "scored against a basis
  this codebase now refuses to score against" (#67).
* **suspect (267 rows)** — an admissible basis (59–66 W) but a stored IF above
  2.0, which is not a hard ride, it is a scoring error. #60 named `if_ > 2.0`
  as the check that would have caught the damage years earlier. Reported apart
  so it cannot quietly distort the ordinary distribution.
* **ordinary (14,588 rows)** — the population the decision is actually about.
* **unscored (21 rows)** — `if_` of 0, never scored; the repair leaves them
  unchanged.

The corrupt and suspect rows are **excluded from both sides** of every
CTL/ATL/TSB figure. Leaving a 16,136,334 TSS row on the "before" side is where
the earlier −33,091 CTL number came from; it describes the corruption, not the
repair.

### Residual damage still inside "ordinary"

58 rows (32 for user 16, 26 for user 19) have an implied basis of 59–66 W with
a stored IF at or below 2.0. They are the same #60 defect, milder, and neither
threshold catches them. They are the reason user 16's ordinary minimum is
−624.6. This is a known, quantified limitation, not a claim that "ordinary" is
perfectly clean; it belongs with the #60 follow-up rather than with a
threshold invented here.

## Per user

```
   uid   rows    chg  skip  preFTP  ord.med   ord.min   ord.max  corrupt  suspect     dCTL     dATL     dTSB
     1    860      7     1     847    +37.4      +0.1     +48.4        0        0    +4.10   +15.69   -11.59
     2    860      7     1     847    +37.4      +0.1     +48.4        0        0    +4.10   +15.69   -11.59
     3    860    858     1     847    -46.2    -124.4      +0.6        0        0   -10.49    -4.08    -6.41
     4    860    858     1     847    -45.6    -122.7      +1.2        0        0    -9.84    -2.36    -7.49
     5    870    860     1     847    +62.1     -60.1    +167.3        0        0   +12.23    +2.68    +9.54
    12    860    859     1     850    +10.5      -8.9     +28.1        0        0    +1.18    -2.99    +4.17
    13    860    859     1     852    +10.5      -8.9     +28.1        0        0    +1.73    -2.28    +4.01
    14    860    859     1     853    +10.6      +0.0     +28.1        0        0    +4.17    +6.27    -2.09
    15    860    859     1     853     +0.6    -107.4     +22.2      686        0   -13.30    -9.42    -3.88
    16    860    859     1     853     +6.4    -624.6     +27.9      453      124    -6.64    -4.70    -1.94
    17    860    859     1     853    +10.2     -45.6     +28.1        0        0    -4.07    -5.66    +1.59
    18    860    859     1     853    +33.0      +0.0     +87.6        0        0   +13.02   +19.54    -6.52
    19    860    859     1     853     +9.6    -293.2     +28.1      171      143    -3.10    -3.26    +0.16
    20    860    859     1     853     +7.7    -162.7     +27.9      379        0   -21.49   -12.78    -8.71
    21    860    859     1     853    +10.5      -8.9     +28.1        0        0    +2.11    -1.66    +3.77
    22    861    860     1     853    +10.5      -8.9     +28.1        0        0    +2.11    -1.66    +3.77
    26    860    859     1     854     +1.5    -132.6     +26.0      646        0   -18.42   -13.95    -4.46
    27    860    859     1     854    +10.5      -8.9     +28.1        0        0    +2.47    -0.89    +3.37
    33    860    859     1     855    +10.5      -8.9     +28.1        0        0    +2.48    -0.88    +3.36
    34    860    859     1     855    +10.5      -8.9     +28.1        0        0    +2.48    -0.88    +3.36
```

`ord.*` covers the ordinary population only. `preFTP` is the count of rows
predating that user's first `ftp_history` entry. `dCTL`/`dATL`/`dTSB` exclude
corrupt and suspect rows on both sides.

### Today's load numbers, before and after

```
   uid              CTL              ATL              TSB
     1    15.7->   19.8    23.6->   39.3    -7.9->  -19.4
     2    15.7->   19.8    23.6->   39.3    -7.9->  -19.4
     3    40.9->   30.4    48.1->   44.0    -7.2->  -13.7
     4    40.9->   31.0    48.1->   45.8    -7.3->  -14.8
     5    25.3->   37.5    35.4->   38.1   -10.1->   -0.6
    12    27.6->   28.8    46.1->   43.1   -18.6->  -14.4
    13    27.0->   28.8    45.4->   43.1   -18.4->  -14.4
    14    24.6->   28.8    36.9->   43.1   -12.3->  -14.4
    15    38.7->   25.4    52.5->   43.1   -13.9->  -17.8
    16    16.6->    9.9    39.6->   34.9   -23.1->  -25.0
    17    32.8->   28.8    48.8->   43.1   -16.0->  -14.4
    18    15.7->   28.8    23.6->   43.1    -7.9->  -14.4
    19    15.6->   12.4    42.8->   39.5   -27.2->  -27.1
    20    48.4->   26.9    55.9->   43.1    -7.6->  -16.3
    21    26.6->   28.8    44.8->   43.1   -18.1->  -14.4
    22    26.6->   28.8    44.8->   43.1   -18.1->  -14.4
    26    44.6->   26.2    57.1->   43.1   -12.5->  -16.9
    27    26.3->   28.8    44.0->   43.1   -17.8->  -14.4
    33    26.3->   28.8    44.0->   43.1   -17.7->  -14.4
    34    26.3->   28.8    44.0->   43.1   -17.7->  -14.4
```

Note what this converges to: after the repair, 13 of the 20 users land on
CTL 28.8 / ATL 43.1 / TSB −14.4 — identical to three significant figures.
That is a consequence of the same 184.9 W basis being applied to what is
evidently the same imported ride history under 13 different accounts. The
figures are correct, but they are figures about a database of cloned test
users, and only users 5, 15, 16, 19, 20 and 26 have histories that differ
materially.

### What each user's rows were scored against (back-solved from `np/if_`)

```
     1 (first ftp_history 2026-07-02): 250W x845, 249W x9, 251W x3, 252W x1, 253W x1
     2 (first ftp_history 2026-07-02): 250W x845, 249W x9, 251W x3, 252W x1, 253W x1
     3 (first ftp_history 2026-07-04): 141W x848, 178W x5, 179W x3, 177W x2, 142W x1
     4 (first ftp_history 2026-07-10): 141W x848, 178W x5, 179W x3, 177W x2, 142W x1
     5 (first ftp_history 2026-07-03): 200W x844, 209W x17, 201W x4, 141W x1, 179W x1
    12 (first ftp_history 2026-07-19): 200W x845, 178W x5, 201W x4, 179W x3, 177W x2
    13 (first ftp_history 2026-07-22): 200W x847, 178W x5, 201W x4, 177W x2, 179W x1
    14 (first ftp_history 2026-07-23): 200W x855, 201W x4
    15 (first ftp_history 2026-07-23): 4W x486, 1W x188, 200W x122, 139W x43, 26W x12
    16 (first ftp_history 2026-07-23): 4W x251, 200W x242, 66W x156, 19W x133, 41W x39
    17 (first ftp_history 2026-07-23): 200W x823, 161W x16, 160W x9, 178W x5, 201W x4
    18 (first ftp_history 2026-07-23): 250W x845, 249W x9, 251W x3, 252W x1, 253W x1
    19 (first ftp_history 2026-07-24): 200W x509, 22W x171, 59W x169, 178W x7, 177W x2
    20 (first ftp_history 2026-07-24): 200W x414, 10W x216, 50W x141, 126W x58, 29W x22
    21 (first ftp_history 2026-07-24): 200W x848, 178W x5, 201W x4, 177W x2
    22 (first ftp_history 2026-07-24): 200W x849, 178W x5, 201W x4, 177W x2
    26 (first ftp_history 2026-07-25): 5W x294, 32W x231, 200W x156, 2W x74, 4W x47
    27 (first ftp_history 2026-07-25): 200W x849, 178W x4, 201W x4, 177W x2
    33 (first ftp_history 2026-07-26): 200W x850, 201W x4, 178W x3, 177W x2
    34 (first ftp_history 2026-07-26): 200W x850, 201W x4, 178W x3, 177W x2
```

This is the single most explanatory table in the report. Users 1, 2 and 18 were
scored at 250 W; users 3 and 4 at 141 W; everyone else mostly at the 200 W
default; users 15, 16, 19, 20 and 26 carry the #60 damage. The direction each
user moves is entirely predicted by their prior basis versus their earliest
recorded FTP.

## Skipped rows

20 rows — exactly one per user — have no usable `start_time` and cannot be
dated, so no historical FTP can be resolved for them. They keep their current
values beside rescored neighbours. The tool now names them:

```
user 1: id 1902   user 2: id 2753    user 3: id 3604    user 4: id 4455
user 5: id 201    user 12: id 5306   user 13: id 6169   user 14: id 7031
user 15: id 7886  user 16: id 9100   user 17: id 9955   user 18: id 10809
user 19: id 11664 user 20: id 12518  user 21: id 13418  user 22: id 14272
user 26: id 17912 user 27: id 18768  user 33: id 19642  user 34: id 20498
```

## The one hand-corrected row

Confirmed, unchanged from the earlier run:

```
user 5 activity 749 (2026-01-01T14:42:41): TSS 84.8 -> 170.4, IF 0.781 -> 1.108
```

It is the only row in the database with an active power-sample correction. The
corrected power itself is preserved (`avg_power`/`np` untouched); only IF and
TSS are rebased. The ride predates user 5's first `ftp_history` entry
(2026-07-03), so it is rescored against their earliest recorded FTP of 141 W
instead of the 200 W it was scored at — hence the near-doubling and an IF above
1.0. This is correct per the stated design, and it is also the clearest single
illustration of what "back-apply the earliest FTP" means in practice: a ride
the rider hand-corrected is relabelled as an above-threshold effort.

## What a rider would actually see

* Most historical rides get a **slightly higher** TSS — typically +10 for a
  ride, up to +48 for users 1/2 and +167 at the extreme for user 5. Roughly 5
  rides in 6 move up.
* Users 3 and 4 are the exception: their rides were scored at 141 W and would
  be rescored at ~178 W, so their historical TSS drops by ~46 per ride and
  their CTL falls from 40.9 to ~30.
* The five users with #60 damage (15, 16, 19, 20, 26) see their impossible
  numbers disappear — 16-million TSS rides become ordinary rides, and IF
  values of 279–330 on the races page become normal. **This is the only
  unambiguous win in the whole change.**
* Everyone's *fitness* number (CTL) moves by roughly ±10 to ±20 and most users
  converge on the same value, because they end up sharing the same 184.9 W
  basis.
* Any ride older than July 2026 — 99% of them — is being re-labelled with an
  FTP that was measured in July 2026. A ride from 2021 is scored as if the
  rider's 2021 fitness equalled their 2026 estimate.

## Recommendation

**Do not run the backfill as a whole. Run the #60 repair only.**

The two populations have completely different cost/benefit:

* **The 2,335 corrupt rows plus 267 suspect rows are unambiguously broken.**
  Fixing them replaces physically impossible values with plausible ones and
  makes five users' load metrics meaningful again. There is no judgement call:
  the current values are wrong under any FTP assumption. This is worth doing.
* **The 12,874 ordinary rows are not broken, they are *stale*.** Rescoring them
  substitutes one debatable FTP assumption (200 W at import time) for another
  (a July 2026 estimate of 184.9 W back-applied to 2021), and moves 10,569 of
  them in the direction opposite to what #54 was approved for. It does not make
  the data more true; it makes it differently assumed. And because the 184.9 W
  figure is itself an unvalidated estimate that 16 of 20 users share verbatim,
  the backfill would bake that estimate into five years of history.

Concretely, if the owner wants to proceed:

1. Settle whether 184.9 W is a real estimate or an artifact of the same import
   path that produced 0.6 W. Until that is answered, "correct" is undefined for
   the ordinary population.
2. Run the repair scoped to the corrupt and suspect populations only. The tool
   does not currently have a `--only` selector; adding one is small and is the
   right next change if this recommendation is accepted.
3. Leave the ordinary population alone, or accept the pre-`ftp_history`
   back-application rule explicitly and in writing first.

If the whole backfill is run anyway, note that the write path has **not** been
exercised at 520 MB scale since these fixes landed (the instruction for this
work was dry-run only). Do a `--write` pass against a copy and diff it before
pointing it at the live database.
