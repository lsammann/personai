# Eval runs

One row per prediction run. `score` appends with `--append --note`.

> **Re-scored 2026-09-14 against corrected ground truth.** Step 7's blind
> recheck exposed the `Personal` precedence rule applied unevenly across nine
> `S_human` rows; seven were corrected (`To Action` 33 -> 29, `Personal`
> 16 -> 20). Every number below is the re-score — no re-inference, the rows in
> `eval/results/` are untouched. What moved is recorded in `docs/PHASE2_PLAN.md`
> -> Step 7 results; the short version is that **`v9b-bookings` still wins on
> the metric with consequences and the `p`-values all got weaker.**

| run | model | prompt | body | split | acc | CI | TA recall | TA retention | gap | note |
|---|---|---|---|---|---|---|---|---|---|---|
| 20260913T065642-2b9cf2 | llama3.1:8b | v1 | 0 | dev | 0.679 | [0.60,0.75] | 7/20 | 16/20 | +0.164 | baseline: sender+subject only, no body. 6 costly errors, the worst of any 8B run |
| 20260913T065929-6ee8cd | llama3.1:8b | v1 | 300 | dev | 0.750 | [0.67,0.81] | 15/20 | 20/20 | +0.143 | body_chars sweep; 17 fixed/7 broken vs 0 (p=0.064) overall, but **8 fixed/0 broken on the 20 To Action messages (p=0.008)** - the body matters, and it matters where it counts |
| 20260913T070800-efdb8d | llama3.1:8b | v1 | 800 | dev | 0.729 | [0.65,0.80] | 11/20 | 18/20 | +0.139 | body_chars sweep; indistinguishable from 300 (5 fixed/2 broken for 300, p=0.45) |
| 20260913T063052-41f083 | llama3.1:8b | v1 | 1500 | dev | 0.714 | [0.63,0.78] | 10/20 | 18/20 | +0.131 | body_chars sweep; DESIGN.md's old default. No better than 300 (8 fixed/3 broken for 300, p=0.23) at 3x the latency |
| 20260913T071831-8d1556 | llama3.1:8b | v1 | 3000 | dev | 0.700 | [0.62,0.77] | 9/20 | 18/20 | +0.172 | body_chars sweep; 300 beats it 9 fixed/2 broken, p=0.065 - the longest setting is now the *worst* 8B body length, at 4x the latency |
| 20260913T075306-327216 | llama3.2:3b | v1 | 1500 | dev | 0.314 | [0.24,0.40] | 0/20 | 20/20 | +0.111 | llama3.2:3b - To Action recall 0/20, predicts Personal for 113 of 200. Retention 20/20 is an artefact: it keeps everything, 97 clutter errors |
| 20260913T080327-6fa8dd | llama3.2:3b | v1 | 800 | dev | 0.414 | [0.34,0.50] | 0/20 | 20/20 | +0.163 | llama3.2:3b at its best body_chars. Still 0/20 on To Action; mean p(To Action) on real bills is 0.050 vs the 8B's 0.549 |
| 20260913T080758-1b5112 | llama3.2:3b | v1 | 800 | dev | 0.421 | [0.34,0.50] | 0/20 | 20/20 | +0.160 | llama3.2:3b permutation screen: 0.443 argmax unchanged, mean TV 0.426. Phase 0 predicted 5/11=0.455 on synthetic mail |
| 20260913T082550-bd6c78 | llama3.1:8b | v1 | 800 | dev | 0.729 | [0.65,0.80] | 11/20 | 18/20 | +0.142 | llama3.1:8b permutation screen: 0.850 unchanged, mean TV 0.167. Phase 0 predicted 9/11=0.818. 3B is disqualified. Stability is label-free, so these two are the only numbers here the re-score did not touch |
| 20260913T105028-6ab3a6 | llama3.1:8b | v2-ordered | 300 | dev | 0.779 | [0.70,0.84] | 14/20 | 20/20 | +0.194 | v2-ordered: precedence as a numbered total order. **Highest 6-way accuracy of any run** - and still rejected: clutter 24 vs v9b's 13, action accuracy 0.821 vs 0.900. Over-applies Personal, which keeps INBOX. 6 fixed/2 broken vs v1, p=0.289 |
| 20260913T105941-b65597 | llama3.1:8b | v9-bookings | 300 | dev | 0.771 | [0.70,0.83] | 12/20 | 20/20 | +0.140 | v9-bookings: Bookings description narrowed. Costly errors 1, clutter 15, action 0.886. 7 fixed/4 broken vs v1, p=0.549 - not significant |
| **20260913T113909-f1f3a4** | llama3.1:8b | **v9b-bookings** | 300 | dev | 0.771 | [0.70,0.83] | 14/20 | **20/20** | +0.157 | **SELECTED.** v9b: v9 minus "appointment", Personal exclusion moved inside the description. Best where it counts - **action accuracy 0.900, clutter 13, 1 costly error, retention 20/20**. 6 fixed/3 broken vs v1 (p=0.508) and 3/3 vs v9 (p=1.00): the effect size is *not* measurable at n=140, and the case for it is the pre-registered Bookings mechanism, not the p-value |
| **20260913T113909-f1f3a4** | llama3.1:8b | **v9b-bookings** | 300 | **holdout** | 0.800 | [0.68,0.88] | 6/9 | **9/9** | +0.166 | **THE LOCK-BOX, opened once 2026-09-14.** Same run, holdout split, n=60. Action accuracy **0.900 - identical to dev** - 1 costly error, clutter 5. No detectable inflation: the point estimate rose rather than fell, though at n=60 the interval overlaps dev almost entirely, so the honest claim is "not detectable at this size". The floor sweep returns identical results at F=0.05/0.10/0.15, replicating the dormancy finding on untouched data. Recall 6/9 carries no claim - pre-registered, 9 To Action messages cannot resolve it. The one costly error is a human-written reply archived as Promotions at 0.925, which is the trigger condition recorded for a Phase 3 Personal round |
| 20260916T124103-b2ea63 | llama3.1:8b | v9b-bookings | 300 | dev | 0.771 | [0.70,0.83] | 14/20 | 20/20 | +0.162 | **Permutation screen on the SHIPPED config**, 600 calls. unchanged 0.853, mean TV 0.155 - better than v1@800's 0.850/0.167, so narrowing the Bookings description cost no robustness. Gate question 4 previously rested on a configuration we do not ship. Also a free reproducibility check: dev accuracy and the action matrix (38/1/13/88) came back identical to 20260913T113909-f1f3a4, three days apart |
| 20260913T114810-1a5257 | llama3.1:8b | v8-updates | 300 | dev | 0.750 | [0.67,0.81] | 12/20 | 19/20 | +0.142 | v8-updates: dropped "low-priority" from the Updates description. FAILED - 2 costly errors, retention 19/20, 3 fixed/3 broken p=1.00. The wording was a labelling problem, not a model one. Do not adopt |
| 20260914T132822-bb1948 | llama3.1:8b | v10-itinerary | 300 | dev | 0.800 | [0.73,0.86] | 15/20 | 20/20 | +0.131 | v10-itinerary: Bookings description told to claim a future-trip ticket even when the email is also the receipt. **REJECTED on pre-registered guards** - six-way accuracy *rose* to the best of any run (4 fixed/0 broken vs v9b, p=0.125) while **action accuracy fell 0.900->0.857 and clutter rose 13->20**. The target message never flipped: United stays Receipts but at 0.752 not 0.970, so its 0 costly errors are Needs Review absorbing it, not comprehension. p(Bookings) on it did rise 0.027->0.238 - mechanism real, insufficient. Needs Review 17.9%->25.7%. Do not adopt |
| 20260913T123421-87a8e6 | llama3.1:8b | v4-format | 300 | dev | 0.757 | [0.68,0.82] | 13/20 | 20/20 | +0.090 | v4-format: named the input fields and stated the From address is not a category. FAILED - both target messages unchanged, and its 0 costly errors are hedging not comprehension: gap +0.157->+0.090 pushed messages into Needs Review. 4 fixed/6 broken vs v9b, p=0.754. Do not adopt |

## The reply rule, added after these runs were scored

Every row above is the **model alone**. The shipped system also runs a
deterministic rule — subject matching `^(Re|Fw|Fwd):` routes to
`Agent/Personal` with `INBOX` retained, no model call — which `score` now
applies from the cached subjects, so every run above re-scores under it for
free. Re-scored across all 14 runs on both splits, 28 scorings: **a costly
error disappeared in 4 and clutter rose in none.**

On the selected run it changes the holdout from action accuracy 0.900 with one
costly error to **0.917 with zero**, and leaves dev untouched. Rule hits are
reported in their own bucket, scored on the action rather than the 6-way label,
and excluded from the calibration table — which is why the selected run's gap
reads +0.162 dev / +0.199 holdout under the rule against +0.157 / +0.166
without it.

## Reading these after the re-score

**The winner did not change, and the reason it wins is now clearer.**
`v2-ordered` has the highest six-way accuracy on the corrected labels (0.779 vs
`v9b`'s 0.771) and is still rejected, because six-way accuracy is not the
metric with consequences: it archives more mail the reader wanted (clutter 24
vs 13) and scores 0.821 on the action matrix against `v9b`'s 0.900. This is the
clearest illustration in the phase of why the collapsed matrix is the one that
decides.

**Every p-value got weaker, and one conclusion changed shape.** `0 -> 300` was
"the single significant comparison in the phase" at p=0.009; on corrected
labels the overall comparison is p=0.064. What survives - and strengthens as an
argument - is the subgroup that matters: **8 fixed / 0 broken on `To Action`,
p=0.008**. The body earns its place by catching bills, not by lifting the
average.

**`To Action` retention is 20/20 on the selected run**, and `To Action` recall
is 14/20 with the `R`-draw soft spot unchanged at 5/9. The denominator fell
from 24 to 20 because seven human-written messages moved to `Personal`; per
`docs/PHASE2_PLAN.md`, recall is now honestly a measurement over *automated*
actionable mail.
