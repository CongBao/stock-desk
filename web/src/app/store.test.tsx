import { render } from '@testing-library/react';

import { useWorkspaceStore } from './store';

function UnboundConsumer() {
  useWorkspaceStore((state) => state.isContextOpen);
  return null;
}

it('rejects workspace consumers outside the required provider boundary', () => {
  const consoleError = vi
    .spyOn(console, 'error')
    .mockImplementation(() => undefined);

  expect(() => render(<UnboundConsumer />)).toThrow(
    'Workspace store must be used inside its provider',
  );

  consoleError.mockRestore();
});
