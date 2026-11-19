import { useLayoutEffect, useRef, useState } from 'react';

/** Follow new steps only while reading the bottom; revealing history never jumps. */
export function useActivityScroll(visible: boolean, conversation: string | undefined, activity: unknown, outcomes: unknown) {
  const scroller = useRef<HTMLElement>(null);
  const following = useRef(true);
  const previous = useRef({ visible: false, conversation });
  const [away, setAway] = useState(false);
  function trailPosition(element: HTMLElement) {
    const trail = element.querySelector('.turn-activity');
    if (!trail) return null;
    const bottom = trail.getBoundingClientRect().bottom - element.getBoundingClientRect().top - element.clientTop;
    return { bottom, target: Math.max(0, Math.min(element.scrollHeight - element.clientHeight, element.scrollTop + bottom - element.clientHeight)) };
  }
  function measure() {
    const element = scroller.current;
    if (!element || !visible) return;
    const distance = element.scrollHeight - element.scrollTop - element.clientHeight;
    const trail = trailPosition(element);
    following.current = distance < 80 || !!trail && Math.abs(trail.bottom - element.clientHeight) < 80;
    setAway(!!trail && (trail.bottom > element.clientHeight + 1 || trail.bottom < 0));
  }
  function jump() {
    const element = scroller.current;
    if (!element) return;
    // Sessions and routing follow the trail. Scrolling past those can hide the latest step.
    element.scrollTop = trailPosition(element)?.target ?? element.scrollHeight;
    following.current = true;
    setAway(false);
  }
  useLayoutEffect(() => {
    const opened = visible && (!previous.current.visible || previous.current.conversation !== conversation);
    previous.current = { visible, conversation };
    if (!visible) return;
    if (opened) {
      if (scroller.current) scroller.current.scrollTop = 0;
      measure();
    } else if (following.current) jump();
    else measure();
  }, [visible, conversation, activity, outcomes]);
  useLayoutEffect(() => {
    const element = scroller.current;
    if (!element || !visible || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(() => { if (following.current) jump(); else measure(); });
    observer.observe(element);
    const body = element.querySelector('.activity');
    if (body) observer.observe(body);
    return () => observer.disconnect();
  }, [visible]);
  return { scroller, away, onScroll: measure, jump };
}
