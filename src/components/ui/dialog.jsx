import React, { useEffect, useRef } from 'react';
import { motion } from 'framer-motion';
import { cn } from '@/lib/utils';

// A modal surface: backdrop, centred panel, the dialog semantics, Escape,
// focus moved in on open and handed back on close, and Tab kept inside.
//
// It renders in place (no portal) because every caller mounts it at the end
// of its page, above the rest of the tree, under an AnimatePresence that
// owns the exit animation; this component only has to be the panel.
//
// `labelledBy` should name the visible title's id so a screen reader
// announces the dialog by it; `initialFocus` is a ref to the control that
// should receive focus first (a text input, the primary button). Without
// one, the panel itself takes focus so the announcement still happens.
const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function Modal({
  open = true,
  onClose,
  labelledBy,
  describedBy,
  initialFocus,
  size = 'md',
  className,
  backdropClassName,
  children,
}) {
  const panelRef = useRef(null);
  const restoreRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    restoreRef.current = document.activeElement;
    const target = initialFocus?.current || panelRef.current;
    // After the entrance frame, so focusing does not fight the transform.
    const id = window.requestAnimationFrame(() => target?.focus?.({ preventScroll: true }));
    return () => {
      window.cancelAnimationFrame(id);
      const previous = restoreRef.current;
      if (previous && typeof previous.focus === 'function' && document.contains(previous)) {
        previous.focus({ preventScroll: true });
      }
    };
  }, [open, initialFocus]);

  function handleKeyDown(event) {
    if (event.key === 'Escape') {
      event.stopPropagation();
      onClose?.();
      return;
    }
    if (event.key !== 'Tab' || !panelRef.current) return;
    const focusable = Array.from(panelRef.current.querySelectorAll(FOCUSABLE));
    if (focusable.length === 0) {
      event.preventDefault();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && (document.activeElement === first || document.activeElement === panelRef.current)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  if (!open) return null;

  const width = { sm: 'max-w-sm', md: 'max-w-md', lg: 'max-w-lg', xl: 'max-w-xl' }[size] || 'max-w-md';

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      className={cn('fixed inset-0 z-50 flex items-center justify-center bg-black/45 px-4', backdropClassName)}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose?.();
      }}
    >
      <motion.div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelledBy}
        aria-describedby={describedBy}
        tabIndex={-1}
        initial={{ scale: 0.96, y: 14 }}
        animate={{ scale: 1, y: 0 }}
        exit={{ scale: 0.96, y: 14 }}
        onKeyDown={handleKeyDown}
        className={cn('w-full outline-none', width, className)}
      >
        {children}
      </motion.div>
    </motion.div>
  );
}
