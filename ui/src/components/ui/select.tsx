/**
 * Select primitive — plain Tailwind wrapper, matching project style (input.tsx, label.tsx).
 * Wraps the native <select> element with the same visual treatment as Input.
 *
 * API surface is intentionally compatible with the subset used by
 * add-subcategory-dialog.tsx so callers can be migrated to a Radix-backed
 * implementation later without changes.
 *
 * Component tree:
 *   <Select value onValueChange>          ← root; holds shared state
 *     <SelectTrigger id>                  ← renders native <select> with collected <option>s
 *       <SelectValue placeholder />       ← no-op (browser handles displayed value)
 *     </SelectTrigger>
 *     <SelectContent>                     ← registers items into Select context; renders nothing
 *       <SelectItem value>label</SelectItem>
 *     </SelectContent>
 *   </Select>
 */
"use client";

import * as React from "react";

import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Internal types
// ---------------------------------------------------------------------------

interface SelectItem {
  value: string;
  label: string;
}

// ---------------------------------------------------------------------------
// Select context — shared between root, trigger and content
// ---------------------------------------------------------------------------

interface SelectContextValue {
  value: string;
  onValueChange: (value: string) => void;
  items: SelectItem[];
  setItems: (items: SelectItem[]) => void;
}

const SelectContext = React.createContext<SelectContextValue>({
  value: "",
  onValueChange: () => undefined,
  items: [],
  setItems: () => undefined,
});

// ---------------------------------------------------------------------------
// Select root
// ---------------------------------------------------------------------------

interface SelectProps {
  value: string;
  onValueChange: (value: string) => void;
  children: React.ReactNode;
}

function Select({ value, onValueChange, children }: SelectProps) {
  const [items, setItems] = React.useState<SelectItem[]>([]);

  return (
    <SelectContext.Provider value={{ value, onValueChange, items, setItems }}>
      {children}
    </SelectContext.Provider>
  );
}

// ---------------------------------------------------------------------------
// SelectTrigger — renders the native <select>; ignores its children visually
// (they exist only for API compatibility with Radix-style usage)
// ---------------------------------------------------------------------------

interface SelectTriggerProps {
  id?: string;
  className?: string;
  // children is accepted for API compatibility with Radix-style usage but not rendered
  children?: React.ReactNode;
}

function SelectTrigger({ id, className }: SelectTriggerProps) {
  const { value, onValueChange, items } = React.useContext(SelectContext);

  // If the items list hasn't been populated yet (SelectContent's
  // useLayoutEffect runs after SelectTrigger's first render) OR if the
  // current value doesn't match any registered item (e.g. after bfcache
  // restore before items rehydrate), render a stand-in <option> so the
  // browser doesn't display an empty native <select>. Keeps the controlled
  // value valid and the trigger visually populated.
  const hasMatch = items.some((i) => i.value === value);
  const placeholderOption = !hasMatch ? (
    <option key="__placeholder" value={value}>{value}</option>
  ) : null;

  return (
    <select
      id={id}
      data-slot="select"
      value={value}
      onChange={(e) => onValueChange(e.target.value)}
      className={cn(
        "flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm shadow-sm",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-ring",
        "disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
    >
      {placeholderOption}
      {items.map((item) => (
        <option key={item.value} value={item.value}>
          {item.label}
        </option>
      ))}
    </select>
  );
}

// ---------------------------------------------------------------------------
// SelectValue — placeholder hint; no-op with native <select>
// ---------------------------------------------------------------------------

// eslint-disable-next-line @typescript-eslint/no-unused-vars
function SelectValue({ placeholder }: { placeholder?: string }) {
  // No-op with native <select> — the browser handles the displayed value.
  return null;
}

// ---------------------------------------------------------------------------
// SelectContent — collects SelectItem children and registers them in context
// ---------------------------------------------------------------------------

interface SelectItemProps {
  value: string;
  children?: React.ReactNode;
}

function SelectContent({ children }: { children: React.ReactNode }) {
  const { setItems } = React.useContext(SelectContext);

  React.useLayoutEffect(() => {
    const collected: SelectItem[] = [];
    React.Children.forEach(children, (child) => {
      if (!React.isValidElement(child)) return;
      const props = (child as React.ReactElement<SelectItemProps>).props;
      if (props.value === undefined) return;
      collected.push({
        value: props.value,
        label:
          typeof props.children === "string"
            ? props.children
            : String(props.children ?? props.value),
      });
    });
    setItems(collected);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [children]);

  // Nothing visible — items surface as <option>s inside SelectTrigger
  return null;
}

// ---------------------------------------------------------------------------
// SelectItem — data-only; rendered as <option> by SelectTrigger via context
// ---------------------------------------------------------------------------

// eslint-disable-next-line @typescript-eslint/no-unused-vars
function SelectItem(_props: SelectItemProps) {
  return null;
}

export {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
};
