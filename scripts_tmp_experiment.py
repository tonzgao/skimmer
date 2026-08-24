"""Experiment results and next steps for classification quality.

Baseline LOO (current code):
  acc 84.3% — but that's below the 90% majority-class trivial baseline.
  must_read recall:        16%   <-- why it feels random: good articles buried
  possible_interest recall:10%
  ignore recall:          92%

Weighted x4 epochs: overall accuracy down (66%), minority recall up
(must_read 27%, skim 19%). Trade-off, not a win.

Diagnosis:
- Only 56 manual labels among 669 examples; implicit labels dominate and are
  noisy (done-in-skimmer with an auto label is weak evidence).
- Extreme imbalance (603/45/21) + hashed feature space (42k weights for 669
  examples) = the model learns "predict ignore" plus a few strong tokens.

Highest-value experiments, in order:
1. Trust re-weighting: manual=1.0, done-with-manual-label=0.9,
   read-elsewhere=0.3 (currently 0.5 flat). The implicit 'ignore' flood is
   drowning the real signal.
2. Two-stage classifier: first ignore vs interesting (well-separated), then
   must_read vs skim only on the survivors. Each stage is less imbalanced.
3. Feature pruning: drop features seen < 3 times; bigrams only from titles.
4. Calibrate: isotonic or Platt scaling on a held-out week so the 0.7
   review threshold means what it says.
5. More signal: time-of-read patterns (weekend vs weekday), feed-level
   base rates as priors rather than learned-from-scratch.
"""
print("see comments")
