import { create } from 'zustand';

type WorkspaceState = {
  readonly closeContext: () => void;
  readonly isContextOpen: boolean;
  readonly openContext: () => void;
};

export const useWorkspaceStore = create<WorkspaceState>((set) => ({
  isContextOpen: false,
  openContext: () => set({ isContextOpen: true }),
  closeContext: () => set({ isContextOpen: false }),
}));
