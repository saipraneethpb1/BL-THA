# Logo

Supplied by Sai Praneeth as the AI Tech Consultant logo (black line art in a circle).

- `logo-mark-*.png`: the line art only, as transparency. The pages use it as a CSS mask
  filled with the text colour, so it reads correctly in both light and dark mode.
- `logo-badge-*.png`: the same lines on a white disc, for the browser tab icon, which can
  sit on any background.

Both are embedded into the pages by `python scripts/sync_suite.py` (see `shared/suite.css`
and `shared/suite-head.js`).
