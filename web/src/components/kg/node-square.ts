import { NodePointProgram } from "sigma/rendering";
import type { Attributes } from "graphology-types";

/**
 * A sigma.js 3 node program that draws a rounded square: sigma's point program (one GL point a node) with a fragment
 * shader that keeps a rounded box instead of a disc. The Entities view draws code nodes with it, so a Directory, File
 * or Symbol never reads as an entity. Needs WebGL: imported by entity-canvas.tsx only, which loads in the browser.
 */
// language=GLSL
const FRAGMENT_SHADER = /*glsl*/ `
precision mediump float;

varying vec4 v_color;
varying float v_border;

const float half_side = 0.42;
const float corner = 0.1;
const vec4 transparent = vec4(0.0, 0.0, 0.0, 0.0);

void main(void) {
  vec2 q = abs(gl_PointCoord - vec2(0.5, 0.5)) - vec2(half_side - corner);
  float dist = corner - length(max(q, 0.0)) - min(max(q.x, q.y), 0.0);

  #ifdef PICKING_MODE
  if (dist > v_border)
    gl_FragColor = v_color;
  else
    gl_FragColor = transparent;

  #else
  float t = 0.0;
  if (dist > v_border)
    t = 1.0;
  else if (dist > 0.0)
    t = dist / v_border;

  gl_FragColor = mix(transparent, v_color, t);
  #endif
}
`;

export default class NodeSquareProgram<
  N extends Attributes = Attributes,
  E extends Attributes = Attributes,
  G extends Attributes = Attributes,
> extends NodePointProgram<N, E, G> {
  override getDefinition() {
    return { ...super.getDefinition(), FRAGMENT_SHADER_SOURCE: FRAGMENT_SHADER };
  }
}
