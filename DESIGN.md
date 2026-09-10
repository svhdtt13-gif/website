# AutoGhostStory Host Configuration UI Design System

## 1. Atmosphere & Identity

This is a quiet operator surface: dense enough for infrastructure facts, but
deliberately calm and fail-closed. The existing signature is dark tonal depth
with cyan operational accents, monospace provenance values, and explicit safety
copy instead of decorative status theatre.

## 2. Color

The feature uses the existing dashboard tokens: `--bg`, `--card`,
`--card-strong`, `--line`, `--text`, `--muted`, `--cyan`, `--green`,
`--amber`, and `--red`. No new palette is introduced.

## 3. Typography

The feature inherits the existing system sans stack. Host IDs, profile IDs,
runtime authority, and response metadata use the existing monospace treatment.
Labels remain compact uppercase dashboard labels.

## 4. Spacing & Layout

Spacing follows the existing 4px rhythm. The configuration panel uses a
two-column form grid above 680px and collapses to one column below 680px.
Confirmation metadata is a readable stacked definition list at every width.

## 5. Components

### Host configuration panel

- **Structure**: heading, safety note, registration form, binding form, result.
- **States**: idle, invalid, confirmation, submitting, success, error.
- **Accessibility**: explicit labels, inline `role=alert` errors, native dialog,
  visible focus, keyboard reachability.
- **Layout**: responsive grid; no runtime controls.

### Confirmation dialog

- **Structure**: operation, exact user values, server-owned safety expectations,
  cancel, confirm.
- **Safety**: requests contain only the backend-approved input fields;
  `runtime_effect`, `runtime_authority`, and binding state are response-only.

## 6. Motion & Interaction

No decorative animation is added. Submit feedback is a status change only and
respects reduced-motion preferences.

## 7. Depth & Surface

Use the existing mixed strategy: tonal gradients for panel identity and subtle
translucent borders for form grouping. No new shadows or glass effects.

## 8. Accessibility Constraints & Accepted Debt

- Target WCAG 2.2 AA with visible focus and readable inline errors.
- Host registration sends exactly `{host_id, display_name}`.
- Host binding sends exactly `{host_id, profile_id}` to `/up/api/host_binding`.
- Successful responses must verify `runtime_effect=NONE`,
  `runtime_authority.mode=LEGACY`, `active_runtime_owner=null`, and binding
  `result.state=OFFLINE` before the UI reports success.
- Successful writes refresh both read-only projections.
- No ACTIVE/runtime/remote/scheduler controls are exposed.
