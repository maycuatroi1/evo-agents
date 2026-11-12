import type { Attributes } from "graphology-types";
import { NodeCircleProgram } from "sigma/rendering";
import type { NodeDisplayData } from "sigma/types";
import { floatColor } from "sigma/utils";

/**
 * A sigma.js 3 node program that draws every node of the knowledge graph's canvases: sigma's disc, with a ring inside
 * its edge when the node's `ring` (a share of the radius) is above 0, in `ringColor`. A hollow node is a disc in the
 * surface colour with a ring. Needs WebGL: imported by sigma-canvas.tsx only, which loads in the browser.
 */
// language=GLSL
const VERTEX_SHADER = /*glsl*/ `
attribute vec4 a_id;
attribute vec4 a_color;
attribute vec2 a_position;
attribute float a_size;
attribute float a_angle;
attribute vec4 a_ringColor;
attribute float a_ring;

uniform mat3 u_matrix;
uniform float u_sizeRatio;
uniform float u_correctionRatio;

varying vec4 v_color;
varying vec4 v_ringColor;
varying vec2 v_diffVector;
varying float v_radius;
varying float v_ring;

const float bias = 255.0 / 254.0;

void main() {
  float size = a_size * u_correctionRatio / u_sizeRatio * 4.0;
  vec2 diffVector = size * vec2(cos(a_angle), sin(a_angle));
  vec2 position = a_position + diffVector;
  gl_Position = vec4((u_matrix * vec3(position, 1)).xy, 0, 1);

  v_diffVector = diffVector;
  v_radius = size / 2.0;
  v_ring = a_ring;

  #ifdef PICKING_MODE
  v_color = a_id;
  v_ringColor = a_id;
  #else
  v_color = a_color;
  v_ringColor = a_ringColor;
  #endif

  v_color.a *= bias;
  v_ringColor.a *= bias;
}
`;

// language=GLSL
const FRAGMENT_SHADER = /*glsl*/ `
precision highp float;

varying vec4 v_color;
varying vec4 v_ringColor;
varying vec2 v_diffVector;
varying float v_radius;
varying float v_ring;

uniform float u_correctionRatio;

const vec4 transparent = vec4(0.0, 0.0, 0.0, 0.0);

void main(void) {
  float smoothing = u_correctionRatio * 2.0;
  float dist = length(v_diffVector);

  #ifdef PICKING_MODE
  if (dist > v_radius)
    gl_FragColor = transparent;
  else
    gl_FragColor = v_color;

  #else
  float inner = v_radius * (1.0 - v_ring);
  float onRing = v_ring > 0.0 ? clamp((dist - inner + smoothing) / smoothing, 0.0, 1.0) : 0.0;
  vec4 colour = mix(v_color, v_ringColor, onRing);
  float outside = clamp((dist - v_radius + smoothing) / smoothing, 0.0, 1.0);
  gl_FragColor = mix(colour, transparent, outside);
  #endif
}
`;

type Ringed = NodeDisplayData & { ringColor?: string; ring?: number };

export default class NodeRingProgram<
  N extends Attributes = Attributes,
  E extends Attributes = Attributes,
  G extends Attributes = Attributes,
> extends NodeCircleProgram<N, E, G> {
  override getDefinition() {
    const base = super.getDefinition();
    return {
      ...base,
      VERTEX_SHADER_SOURCE: VERTEX_SHADER,
      FRAGMENT_SHADER_SOURCE: FRAGMENT_SHADER,
      ATTRIBUTES: [
        ...base.ATTRIBUTES,
        { name: "a_ringColor", size: 4, type: WebGLRenderingContext.UNSIGNED_BYTE, normalized: true },
        { name: "a_ring", size: 1, type: WebGLRenderingContext.FLOAT },
      ],
    };
  }

  override processVisibleItem(nodeIndex: number, startIndex: number, data: NodeDisplayData) {
    const array = this.array;
    const ringed = data as Ringed;
    const ring = Math.min(1, Math.max(0, ringed.ring ?? 0));
    let at = startIndex;
    array[at++] = data.x;
    array[at++] = data.y;
    array[at++] = data.size;
    array[at++] = floatColor(data.color);
    array[at++] = nodeIndex;
    array[at++] = floatColor(ringed.ringColor ?? data.color);
    array[at++] = ring;
  }
}
