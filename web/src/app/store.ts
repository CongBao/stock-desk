import { createContext, useContext } from 'react';
import { createStore, useStore, type StoreApi } from 'zustand';

export type WorkspaceState = {
  readonly closeContext: () => void;
  readonly isContextOpen: boolean;
  readonly openContext: () => void;
};

export function createWorkspaceStore(): StoreApi<WorkspaceState> {
  return createStore<WorkspaceState>((set) => ({
    isContextOpen: false,
    openContext: () => set({ isContextOpen: true }),
    closeContext: () => set({ isContextOpen: false }),
  }));
}

export const WorkspaceStoreContext = createContext<
  StoreApi<WorkspaceState> | undefined
>(undefined);

export function useWorkspaceStore<T>(
  selector: (state: WorkspaceState) => T,
): T {
  const store = useContext(WorkspaceStoreContext);

  if (!store) {
    throw new Error('Workspace store must be used inside its provider');
  }

  return useStore(store, selector);
}
