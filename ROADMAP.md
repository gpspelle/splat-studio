# Roadmap

Three phases, each a release. A phase starts when the gate before it is met, so new features
never pile up on an unreliable core. Status lives in the issues and their milestones.

| Release | Theme | Gate to move on |
| --- | --- | --- |
| [v0.4](https://github.com/gpspelle/splat-studio/milestone/1) | Make the betas solid | Choose what to keep and several objects work on real videos |
| [v0.5](https://github.com/gpspelle/splat-studio/milestone/2) | Cleaner objects, ready to share | A clean object per video, first try |
| [v1.0](https://github.com/gpspelle/splat-studio/milestone/3) | From one object to a catalogue | |

## How work is tracked

- One issue per feature or bug, small enough for one pull request, with acceptance criteria.
- Labels: `kind:` (feature, bug, chore, docs), `area:` (capture, selection, training, library,
  viewer, export, ui), `size:` (S, M, L), and `beta` for what still needs a real-world test.
- A branch `feature/<issue>-<slug>` opens a pull request that says `Closes #<issue>`.
- Ideas that aren't decided yet go to Discussions first.
