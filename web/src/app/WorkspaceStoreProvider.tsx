import { useState, type ReactNode } from 'react';

import { createWorkspaceStore, WorkspaceStoreContext } from './store';

type WorkspaceStoreProviderProps = {
  readonly children: ReactNode;
};

export function WorkspaceStoreProvider({
  children,
}: WorkspaceStoreProviderProps) {
  const [store] = useState(createWorkspaceStore);

  return (
    <WorkspaceStoreContext.Provider value={store}>
      {children}
    </WorkspaceStoreContext.Provider>
  );
}
