import { useCallback, useRef, useState, type JSX } from "react";
import { ConfirmDialog, type ConfirmOptions } from "../components/Dialog";

// `const [confirm, dialog] = useConfirm();` then `if (await confirm({...})) ...`
// and render `{dialog}`. Resolves false on Cancel, Escape, or a scrim click.
export function useConfirm(): [(options: ConfirmOptions) => Promise<boolean>, JSX.Element | null] {
  const [options, setOptions] = useState<ConfirmOptions | null>(null);
  const resolver = useRef<((ok: boolean) => void) | null>(null);

  const confirm = useCallback((next: ConfirmOptions) => {
    setOptions(next);
    return new Promise<boolean>((resolve) => {
      resolver.current = resolve;
    });
  }, []);

  const dialog = options ? (
    <ConfirmDialog
      options={options}
      onResult={(ok) => {
        setOptions(null);
        resolver.current?.(ok);
        resolver.current = null;
      }}
    />
  ) : null;

  return [confirm, dialog];
}
