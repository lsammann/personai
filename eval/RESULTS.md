# Eval runs

One row per prediction run. `score` appends with `--append --note`.

| run | model | prompt | body | split | acc | CI | TA recall | TA retention | gap | note |
|---|---|---|---|---|---|---|---|---|---|---|
| 20260913T065642-2b9cf2 | llama3.1:8b | v1 | 0 | dev | 0.664 | [0.58,0.74] | 7/24 | 18/24 | +0.172 | baseline: sender+subject only, no body |
| 20260913T065929-6ee8cd | llama3.1:8b | v1 | 300 | dev | 0.764 | [0.69,0.83] | 18/24 | 22/24 | +0.149 | body_chars sweep; 20 fixed/6 broken vs 0 (p=0.009), 11/0 on To Action (p=0.001) - the body matters |
| 20260913T070800-efdb8d | llama3.1:8b | v1 | 800 | dev | 0.743 | [0.66,0.81] | 14/24 | 20/24 | +0.149 | body_chars sweep; indistinguishable from 300 (2 fixed/5 broken, p=0.45) |
| 20260913T063052-41f083 | llama3.1:8b | v1 | 1500 | dev | 0.743 | [0.66,0.81] | 14/24 | 20/24 | +0.130 | body_chars sweep; the current DESIGN.md default. No better than 300 (4 fixed/7 broken, p=0.55) at 3x the latency |
| 20260913T071831-8d1556 | llama3.1:8b | v1 | 3000 | dev | 0.729 | [0.65,0.80] | 13/24 | 20/24 | +0.172 | body_chars sweep; no gain over 300 (3 fixed/8 broken, p=0.23) at 4x the latency |
| 20260913T075306-327216 | llama3.2:3b | v1 | 1500 | dev | 0.286 | [0.22,0.37] | 0/24 | 23/24 | +0.091 | llama3.2:3b - To Action recall 0/24, predicts Personal for 113 of 200. Not competitive |
| 20260913T080327-6fa8dd | llama3.2:3b | v1 | 800 | dev | 0.393 | [0.32,0.48] | 0/24 | 23/24 | +0.149 | llama3.2:3b at its best body_chars. Still 0/24 on To Action; mean p(To Action) on real bills is 0.050 vs the 8B's 0.549 |
| 20260913T080758-1b5112 | llama3.2:3b | v1 | 800 | dev | 0.400 | [0.32,0.48] | 0/24 | 23/24 | +0.147 | llama3.2:3b permutation screen: 0.443 argmax unchanged, mean TV 0.426. Phase 0 predicted 5/11=0.455 on synthetic mail |
| 20260913T082550-bd6c78 | llama3.1:8b | v1 | 800 | dev | 0.743 | [0.66,0.81] | 14/24 | 20/24 | +0.148 | llama3.1:8b permutation screen: 0.850 unchanged, mean TV 0.167. Phase 0 predicted 9/11=0.818. 3B is disqualified |
| 20260913T105028-6ab3a6 | llama3.1:8b | v2-ordered | 300 | dev | 0.793 | [0.72,0.85] | 17/24 | 22/24 | +0.188 | v2-ordered: precedence as a numbered total order. Bookings over-prediction 21->13, but Personal over-corrects to 25 (truth 16) so clutter 17->24 and action accuracy DROPS 0.836->0.800. 6 fixed/2 broken vs v1, p=0.289 - not significant |
| 20260913T105941-b65597 | llama3.1:8b | v9-bookings | 300 | dev | 0.793 | [0.72,0.85] | 15/24 | 23/24 | +0.152 | v9-bookings: Bookings description narrowed. Bookings 21->13, Personal lands exactly on 16, costly errors 6->3, action accuracy 0.836->0.857 at no extra clutter. 8 fixed/4 broken vs v1, p=0.388 - NOT significant; mechanism confirmed, effect size is not |
| 20260913T113909-f1f3a4 | llama3.1:8b | v9b-bookings | 300 | dev | 0.800 | [0.73,0.86] | 18/24 | 23/24 | +0.162 | v9b-bookings: v9 minus the word 'appointment' (which appeared in the subject it still got wrong) plus the Personal exclusion moved INSIDE the description. Best on every axis: acc 0.800, action 0.879, TA recall back to 18/24, Bookings 21->10. 7 fixed/2 broken vs v1, p=0.180 - still not significant |
| 20260913T114810-1a5257 | llama3.1:8b | v8-updates | 300 | dev | 0.764 | [0.69,0.83] | 15/24 | 21/24 | +0.153 | v8-updates: dropped 'low-priority' from the Updates description. FAILED - costly errors 6->7, Updates predictions unchanged at 6 (truth 21), 3 fixed/3 broken p=1.00. The wording was a labelling problem, not a model one. Do not adopt |
| 20260913T123421-87a8e6 | llama3.1:8b | v4-format | 300 | dev | 0.779 | [0.70,0.84] | 16/24 | 24/24 | +0.102 | v4-format: named the input fields and stated the From address is not a category. FAILED - both target messages (Receipts@united.com, bookings@anaesthesia-analgesia) unchanged. Apparent 0 costly errors is hedging, not comprehension: calibration gap +0.162->+0.102 pushed 7 more messages into Needs Review. 4 fixed/7 broken vs v9b, p=0.549. Do not adopt |
