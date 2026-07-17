export function BrandIcon({ className }: { readonly className?: string }) {
  return (
    <img
      className={className}
      src="/brand-icon.svg"
      alt="Stock Desk"
      draggable={false}
    />
  );
}
