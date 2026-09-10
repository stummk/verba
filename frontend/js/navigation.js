// Which navigation owns the view right now.
//
// Every view renders asynchronously and writes into the one #view element
// itself, so the router's check after `await render()` comes too late: a
// render the next navigation has overtaken has by then already painted its
// own content over the view that replaced it — and dropped its late answers
// into the module state of the view that took over. Hence a token: the router
// starts a navigation, every render takes the guard as its first act and stops
// as soon as it no longer owns the view.

let generation = 0;

/** The router: a new navigation begins, and this is its token. */
export function beginNavigation() {
  return ++generation;
}

/** The router: does this token still own the view? */
export function isCurrent(token) {
  return token === generation;
}

/**
 * A view: the guard for this render, to be taken before the first `await`.
 * Answers false once another navigation has taken the view over.
 */
export function viewGuard() {
  const token = generation;
  return () => token === generation;
}
