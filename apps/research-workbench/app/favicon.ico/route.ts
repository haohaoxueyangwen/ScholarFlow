// Some browsers still request the conventional URL even with icon metadata.
// Serve the user's selected, audited product image without duplicating it.
export function GET(): Response {
  return new Response(null, { status: 302, headers: { Location: "/brand/scholarflow-icon.png", "Cache-Control": "public, max-age=3600" } });
}
