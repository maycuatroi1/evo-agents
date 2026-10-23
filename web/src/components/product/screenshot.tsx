import { IMAGE_SIZES, imagePath, type ProductImage, type Scheme } from "./images";

/**
 * Below this width the page shows the phone crop (DESIGN.md's 768 px breakpoint). The crops are static files that
 * scripts/product-images.mjs already sized and compressed, so a plain <img> in a <picture> serves them as they are.
 */
const PHONE = "(max-width: 767px)";

function Picture({ image, scheme, alt, className }: { image: ProductImage; scheme: Scheme; alt: string; className: string }) {
  const { wide, phone } = IMAGE_SIZES;
  return (
    <picture className={className}>
      <source media={PHONE} srcSet={imagePath(image, "phone", scheme)} width={phone.width} height={phone.height} />
      <img
        src={imagePath(image, "wide", scheme)}
        width={wide.width}
        height={wide.height}
        alt={alt}
        loading="lazy"
        decoding="async"
        className="block h-auto w-full"
      />
    </picture>
  );
}

/**
 * A screen of the demo hub in the page's theme: the light copy in light, the dark one in dark (the hidden one is never
 * fetched, being lazy and not displayed), each a phone crop under 768 px and the wide shot above.
 */
export function Screenshot({ image, alt, testId }: { image: ProductImage; alt: string; testId?: string }) {
  return (
    <div className="min-w-0 self-start overflow-hidden rounded-md border bg-card shadow-raised" data-testid={testId}>
      <Picture image={image} scheme="light" alt={alt} className="block dark:hidden" />
      <Picture image={image} scheme="dark" alt={alt} className="hidden dark:block" />
    </div>
  );
}
