/**
 * §65 — the global keyboard vocabulary + the shortcut registry.
 *
 * ONE source of truth for the help overlay: `SHORTCUTS` is the same table the
 * dispatcher implements, so the `?` overlay is structurally incapable of
 * drifting from real bindings — if a binding changes, the table changes with
 * it, and the overlay is "obviously wrong when stale" rather than silently
 * divergent.
 *
 * Lists (decisions tape, journal order decisions, mint-history drill-down)
 * register a controller here on mount and unregister on unmount. Because only
 * the active tab's components are mounted, the LAST registered list is the
 * current one — the global dispatcher drives it. Lists that own their own
 * scoped key handler (the tape keeps §63's focused-listbox keys) also expose
 * `owns()` so the dispatcher stands down while the list itself has focus and
 * never double-steps a keypress.
 *
 * GUARDS (the standard failure mode for global shortcuts is firing while
 * someone types): the dispatcher skips keys when the palette is open, when a
 * help overlay is up, when focus sits in any text field (input/textarea/
 * select/contentEditable), and for every chord with meta/ctrl/alt.
 */

export interface ListController {
  /** Registry id, e.g. 'feed', 'journal', 'mint-history'. */
  id: string
  /** True when the element is inside this list's own container (the list
   *  handles its own keys while focused — the dispatcher must stand down). */
  owns?: (el: Element | null) => boolean
  length: () => number
  moveDown(): void
  moveUp(): void
  jumpTop(): void
  jumpBottom(): void
  /** Expand / collapse the cursor row (the existing expand mechanism). */
  toggle(): void
  /** Collapse the expanded row. Returns true if something was collapsed —
   *  Esc's first duty — so callers know to fall through to backing out. */
  collapse(): boolean
}

const registry = new Map<string, { current: ListController }>()

/** Register the mounted list; returns its unregister function. */
export function registerList(
  id: string,
  ref: { current: ListController },
): () => void {
  registry.set(id, ref)
  return () => {
    if (registry.get(id) === ref) registry.delete(id)
  }
}

/** The currently mounted list — the last one registered (mount order). */
export function activeList(): ListController | null {
  const ids = [...registry.keys()]
  for (let i = ids.length - 1; i >= 0; i--) {
    const ref = registry.get(ids[i])
    if (ref?.current) return ref.current
  }
  return null
}

/** Is focus inside a typing context? j/k/g/G/Enter must never fire there. */
export function isTextTarget(): boolean {
  const el = document.activeElement
  if (!el) return false
  const tag = el.tagName
  return (
    tag === 'INPUT' ||
    tag === 'TEXTAREA' ||
    tag === 'SELECT' ||
    (el instanceof HTMLElement && el.isContentEditable)
  )
}

/** The full vocabulary — rendered verbatim by the `?` overlay. */
export interface ShortcutDef {
  keys: string
  what: string
  where: string
}

export const SHORTCUTS: ShortcutDef[] = [
  { keys: '⌘K / Ctrl-K', what: 'open / close the command palette', where: 'anywhere' },
  { keys: 'j / k', what: 'move the cursor down / up the current list', where: 'tape · journal · mint history' },
  { keys: 'g', what: 'jump to the top of the current list', where: 'tape · journal · mint history' },
  { keys: 'G', what: 'jump to the bottom of the current list', where: 'tape · journal · mint history' },
  { keys: 'Enter', what: 'expand / collapse the focused row', where: 'tape · journal · mint history' },
  { keys: 'Esc', what: 'collapse the focused row · back out of mint history · close overlays', where: 'anywhere' },
  { keys: '?', what: 'show / hide this help', where: 'anywhere (never while typing)' },
]
