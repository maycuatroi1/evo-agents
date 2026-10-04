import { notFound } from "next/navigation";

/** Any other path, for a signed-in visitor: the not-found state inside the shell rather than a bare 404 page. */
export default function Missing() {
  notFound();
}
