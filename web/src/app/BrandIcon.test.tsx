import { render, screen } from '@testing-library/react';

import { BrandIcon } from './BrandIcon';

it('renders the canonical public brand icon', () => {
  render(<BrandIcon />);

  expect(screen.getByRole('img', { name: 'Stock Desk' })).toHaveAttribute(
    'src',
    '/brand-icon.svg',
  );
});
