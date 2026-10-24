// cytoscape-fcose ships no types: it is a Cytoscape extension, registered with `cytoscape.use`.
declare module "cytoscape-fcose" {
  import type { Ext } from "cytoscape";

  const fcose: Ext;
  export default fcose;
}
