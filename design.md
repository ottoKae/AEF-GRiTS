# Design: AEF-GRiTS download workbench

The shared system for `/` and `/portal`. Change it deliberately; neither view
changes the download, authentication or planning contract through presentation.

## System

- Genre: modern-minimal; scope: app screen, not a marketing page.
- Theme: Dose, requested by the user on 2026-10-30.
- Material: warm pale paper, green-black ink and softly tinted functional modules.
- Signature: the spatial map beside the download configuration and collapsible task dock.
- Shell: shared header, existing left-hand workflow, map and bottom task/results area.
- Keep workflow order: connection, position, years, storage; do not add a hero or sales content.

## Tokens

`webapp/static/tokens.css` is the source of truth for palette, fonts, spacing and layers.

- Paper: Dose warm beige (`--color-paper`); secondary surfaces are pale green.
- Ink: `--color-ink`, `--color-ink-2`, `--color-muted`.
- Primary action, selection and focus: pharmacy green and its paired light ink.
- Header is one compact paper toolbar, not four filled cards. A green wordmark and
  lightly tinted product selector establish hierarchy; status and utilities stay quiet.
  Sidebar section tints remain unchanged and are not verification indicators.
- Local Chinese/system sans fonts are used for offline reliability. No remote font request.
- Header: 24 px wordmark, 18 px product name, 16 px status/actions and supporting metadata.
- Body: 16 px; labels use the shared small/body steps, not tracked capitals.
- Local SVG controls use `currentColor`; no emoji icon dependency.
- Map AOI, selected-grid and point colours remain semantic map tokens.

## Layers and interaction

- The header is above an isolated main/map stacking context. Leaflet's internal
  pane and control indices cannot cover the product selector.
- Product menu stays within the viewport and Escape closes it and restores focus.
- Native dialog overlays remain above both header and map.
- Task/results collapse hides presentation only. Polling, plan state and filters survive.
- Instant visible focus, native disabled controls and reduced-motion behavior remain.

## Exports and verification

Theme tokens: `webapp/static/tokens.css`; app baseline: `webapp/static/base.css`.
Application rules remain in `webapp/static/index.html` and `webapp/static/portal.css`.
Verify both views with the existing browser tests and `tests/test_webapp_dose.py`.
Keep real credentials and download output out of screenshot fixtures.
