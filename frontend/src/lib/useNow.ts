import { useEffect, useState } from 'react'

/** A shared ticking clock, so "as of" stamps and ages move without every component
 * keeping its own interval. */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs)
    return () => window.clearInterval(timer)
  }, [intervalMs])
  return now
}
