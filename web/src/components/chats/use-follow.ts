"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

/** How close to the end the region must be for a new message to keep it there. */
const NEAR_PX = 96;

/**
 * Keeps a scroll region of messages (the element `region` is put on) at its newest end while the visitor reads there:
 * it opens there, and when what it shows changes (`count` messages, or anything else `tick` counts, such as a
 * reaction) it follows, unless the visitor scrolled up to read; then a new message sets `behind`, and the page offers
 * Jump to the latest. Messages added before the first (an older page) keep the one in view where it is; the region
 * sets `overflow-anchor: none`, so the browser does not move it as well.
 */
export function useFollow(count: number, firstKey: number | null, tick = 0) {
  const region = useRef<HTMLDivElement>(null);
  const [behind, setBehind] = useState(false);
  const near = useRef(true);
  const opened = useRef(false);
  const last = useRef({ height: 0, top: 0, firstKey, count });

  const toEnd = useCallback(() => {
    region.current?.scrollTo({ top: region.current.scrollHeight });
    near.current = true;
    setBehind(false);
  }, []);

  useEffect(() => {
    const element = region.current;
    if (!element) return;
    const onScroll = () => {
      near.current = element.scrollHeight - element.scrollTop - element.clientHeight <= NEAR_PX;
      last.current = { ...last.current, height: element.scrollHeight, top: element.scrollTop };
      if (near.current) setBehind(false);
    };
    element.addEventListener("scroll", onScroll, { passive: true });
    return () => element.removeEventListener("scroll", onScroll);
  }, []);

  useLayoutEffect(() => {
    const element = region.current;
    if (!element) return;
    const before = last.current;
    if (!opened.current) {
      opened.current = true;
      element.scrollTo({ top: element.scrollHeight });
    } else if (before.firstKey !== firstKey && before.firstKey !== null) {
      // An older page went in above: keep what the visitor was reading in place.
      element.scrollTo({ top: before.top + (element.scrollHeight - before.height) });
    } else if (near.current) {
      element.scrollTo({ top: element.scrollHeight });
    } else if (count > before.count) {
      setBehind(true);
    }
    last.current = { height: element.scrollHeight, top: element.scrollTop, firstKey, count };
  }, [count, firstKey, tick]);

  return { region, behind, toEnd };
}
