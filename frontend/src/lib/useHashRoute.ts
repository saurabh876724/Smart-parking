import { useCallback, useEffect, useState } from 'react'
import { DEFAULT_VIEW, hashForView, viewFromHash, type ViewId } from './nav'

/** Which page is showing, kept in the URL fragment.
 *
 * A hash rather than a router dependency: the fragment is what makes a page
 * shareable for a demo, survives a reload on a kiosk, and needs no server rewrite
 * rule behind nginx - which this app does not have.
 */
export function useHashRoute(): [ViewId, (id: ViewId) => void] {
  const [view, setView] = useState<ViewId>(() =>
    typeof window === 'undefined' ? DEFAULT_VIEW : viewFromHash(window.location.hash),
  )

  useEffect(() => {
    const onHashChange = () => setView(viewFromHash(window.location.hash))
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  const navigate = useCallback((id: ViewId) => {
    const next = hashForView(id)
    if (window.location.hash === next) setView(id)
    else window.location.hash = next
  }, [])

  return [view, navigate]
}
