import { mkdir, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';

const root = resolve(process.argv[2] || 'docs/test-results/20261117-png-shader-recall/fixtures');
const header = parameters => `#version 300 es
// == ShaderGen params ==
${parameters.map(p => `// param: ${p.name} | type: float | min: ${p.min} | max: ${p.max} | default: ${p.value} | label: ${p.label}`).join('\n')}
// == end params ==
precision highp float;
uniform vec2 u_resolution;
${parameters.map(p => `uniform float ${p.name};`).join('\n')}
out vec4 color;
`;

const shaders = [
  {
    id: 'T01', parameters: [
      { name: 'u_glow', min: 0.01, max: 0.16, value: 0.07, label: 'Glow spread' },
      { name: 'u_energy', min: 0.3, max: 2.0, value: 1.0, label: 'Glow energy' },
    ],
    body: `
float star(vec2 p) {
    float distance2 = 100.0;
    float signValue = 1.0;
    for (int i = 0; i < 10; i++) {
        float a = 1.5707963 + float(i) * 0.6283185;
        float b = a + 0.6283185;
        float ra = i % 2 == 0 ? 0.125 : 0.061;
        float rb = i % 2 == 0 ? 0.061 : 0.125;
        vec2 va = ra * vec2(cos(a), sin(a));
        vec2 vb = rb * vec2(cos(b), sin(b));
        vec2 edge = vb - va;
        vec2 q = p - va;
        vec2 nearest = q - edge * clamp(dot(q, edge) / dot(edge, edge), 0.0, 1.0);
        distance2 = min(distance2, dot(nearest, nearest));
        if ((va.y > p.y) != (vb.y > p.y) && p.x < va.x + (p.y - va.y) * edge.x / edge.y) signValue *= -1.0;
    }
    return signValue * sqrt(distance2);
}
float heart(vec2 p) {
    p.y = -p.y;
    p.x = abs(p.x);
    if (p.y + p.x > 0.075) return length(p - vec2(0.035, 0.055)) - 0.066;
    return length(p - vec2(0.0, -0.025)) - 0.091;
}
vec3 emitter(float d, vec3 tint) {
    float core = 1.0 - smoothstep(-0.002, 0.002, d);
    float bloom = exp(-max(d, 0.0) / u_glow);
    return core * mix(tint, vec3(1.0), 0.80) + u_energy * tint * (0.36 * bloom + 0.10 * exp(-max(d, 0.0) / (u_glow * 2.2)));
}
void main() {
    vec2 p = (gl_FragCoord.xy - 0.5 * u_resolution) / u_resolution.y;
    vec3 rgb = emitter(star(p - vec2(0.17, 0.34)), vec3(1.0, 0.50, 0.12));
    rgb += emitter(length(p) - 0.103, vec3(1.0, 0.24, 0.90));
    rgb += emitter(heart(p - vec2(-0.20, -0.35)), vec3(0.13, 0.72, 1.0));
    color = vec4(rgb, 1.0);
}
`,
    defaults: [0.03, 0.07, 0.10, 0.045, 0.065, 0.14].map(u_glow => ({ u_glow })),
    regression: ['float bloom = exp(-max(d, 0.0) / u_glow);', 'float bloom = exp(-max(d, 0.0) / (u_glow * 3.5));'],
  },
  {
    id: 'T02', parameters: [
      { name: 'u_density', min: 8, max: 44, value: 25, label: 'Filament frequency' },
      { name: 'u_energy', min: 0.2, max: 2, value: 0.9, label: 'Emission' },
    ],
    body: `
float randomValue(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
float noise(vec2 p) {
    vec2 cell = floor(p), f = fract(p); f = f * f * (3.0 - 2.0 * f);
    return mix(mix(randomValue(cell), randomValue(cell + vec2(1,0)), f.x),
        mix(randomValue(cell + vec2(0,1)), randomValue(cell + vec2(1,1)), f.x), f.y);
}
void main() {
    vec2 p = (gl_FragCoord.xy - 0.5 * u_resolution) / u_resolution.y;
    float r = length(p);
    float warp = noise(p * 11.0) + 0.5 * noise(p * 27.0);
    float field = 0.0;
    for (int i = 0; i < 5; i++) {
        float angle = float(i) * 1.256637;
        vec2 q = mat2(cos(angle), -sin(angle), sin(angle), cos(angle)) * p;
        float curve = abs(sin(u_density * (q.x + 0.11 * sin(q.y * 15.0 + warp * 6.0))));
        field += exp(-curve * 12.0) * (0.4 + noise(q * 100.0));
    }
    float envelope = exp(-pow(r / 0.34, 4.0));
    vec3 tint = mix(vec3(0.0, 0.10, 1.0), vec3(0.0, 0.95, 1.0), smoothstep(0.0, 0.36, r));
    vec3 rgb = u_energy * tint * envelope * field + vec3(0.0,0.0,0.10) * envelope;
    color = vec4(rgb, 1.0);
}
`,
    defaults: [14, 25, 34, 18, 28, 11].map(u_density => ({ u_density })),
    regression: ['field += exp(-curve * 12.0)', 'field += exp(-curve * 1.5)'],
  },
  {
    id: 'T03', parameters: [
      { name: 'u_glow', min: 0.006, max: 0.08, value: 0.026, label: 'Ribbon softness' },
      { name: 'u_bend', min: 0.02, max: 0.25, value: 0.10, label: 'Ribbon bend' },
    ],
    body: `
void main() {
    vec2 p = (gl_FragCoord.xy - 0.5 * u_resolution) / u_resolution.y;
    vec3 rgb = vec3(0.025, 0.025, 0.075);
    rgb += vec3(0.04,0.0,0.10) * exp(-length(p - vec2(0.65,0.20)));
    for (int i = 0; i < 12; i++) {
        float phase = float(i) * 0.43;
        float wave = -0.02 + u_bend * sin(p.x * 4.0 + phase) + 0.025 * sin(p.x * 17.0 + phase * 1.7);
        wave += 0.035 * sin(phase * 1.9) + 0.07 * p.x;
        float d = abs(p.y - wave);
        vec3 tint = 0.5 + 0.5 * cos(vec3(0.0,2.0,4.0) + phase + p.x * 2.0);
        float ribbon = exp(-d / u_glow);
        rgb += tint * (0.18 * ribbon + 0.025 * exp(-d / (u_glow * 3.0)));
    }
    color = vec4(rgb,1.0);
}
`,
    defaults: [0.05, 0.026, 0.018, 0.04, 0.022, 0.07].map(u_glow => ({ u_glow })),
    regression: ['float ribbon = exp(-d / u_glow);', 'float ribbon = 1.0 - smoothstep(0.0, 0.10, d);'],
  },
  {
    id: 'T04', parameters: [
      { name: 'u_scale', min: 0.016, max: 0.028, value: 0.0222, label: 'Heart size' },
      { name: 'u_shade', min: 0.1, max: 1.2, value: 0.72, label: 'Material shading' },
    ],
    body: `
vec2 heartPoint(float t) {
    return u_scale * vec2(16.0 * (sin(t) * sin(t) * sin(t)),
        13.0*cos(t)-5.0*cos(2.0*t)-2.0*cos(3.0*t)-cos(4.0*t));
}
float heartDistance(vec2 p) {
    float nearest = 100.0, side = 1.0;
    for (int i=0; i<64; i++) {
        vec2 a=heartPoint(float(i)*6.2831853/64.0);
        vec2 b=heartPoint(float(i+1)*6.2831853/64.0);
        vec2 e=b-a, q=p-a;
        vec2 v=q-e*clamp(dot(q,e)/dot(e,e),0.0,1.0);
        nearest=min(nearest,dot(v,v));
        if ((a.y>p.y)!=(b.y>p.y) && p.x<a.x+(p.y-a.y)*e.x/e.y) side=-side;
    }
    return side*sqrt(nearest);
}
void main() {
    vec2 p=(gl_FragCoord.xy-0.5*u_resolution)/u_resolution.y;
    float d=heartDistance(p-vec2(0.0,0.01));
    vec3 background=mix(vec3(0.68,0.51,0.42),vec3(0.91,0.71,0.60),exp(-dot(p,p)*2.0));
    float shade = 0.72 + u_shade * p.x;
    vec3 heartColor=vec3(0.88,0.025,0.13)*shade + vec3(0.0,0.025,0.01)*(0.5-p.y);
    color=vec4(mix(background,heartColor,1.0-smoothstep(-0.002,0.002,d)),1.0);
}
`,
    defaults: [0.3, 0.72, 1.0, 0.45, 0.8, 0.15].map(u_shade => ({ u_shade })),
    regression: ['float shade = 0.72 + u_shade * p.x;', 'float shade = 0.72 - u_shade * p.y;'],
  },
  {
    id: 'T05', parameters: [
      { name: 'u_width', min: 0.02, max: 0.13, value: 0.065, label: 'Ring width' },
      { name: 'u_shine', min: 0.1, max: 1.5, value: 0.9, label: 'Metal highlights' },
    ],
    body: `
void main() {
    vec2 uv=gl_FragCoord.xy/u_resolution;
    vec3 rgb=vec3(0.085,0.035,0.025);
    for(int i=0;i<4;i++) {
        vec2 center=i==0?vec2(0.35,0.72):i==1?vec2(0.82,1.04):i==2?vec2(0.10,0.22):vec2(0.77,-0.13);
        vec2 extent=i%2==0?vec2(0.24,0.22):vec2(0.29,0.24);
        vec2 p=(uv-center)/extent;
        float r=length(p), t=(r-1.0)/u_width;
        float mask=1.0-smoothstep(0.90,1.0,abs(t));
        float z=sqrt(max(0.0,1.0-t*t));
        vec3 normal=normalize(vec3(normalize(p+0.0001)*t,z*1.4));
        vec3 reflected=reflect(vec3(0,0,-1),normal);
        float spec=pow(max(0.0,dot(reflected,normalize(vec3(-0.5,0.8,1.0)))),20.0);
        spec+=0.5*pow(max(0.0,dot(reflected,normalize(vec3(0.5,-0.2,1.0)))),35.0);
        float fine=pow(0.5+0.5*sin(t*24.0+atan(p.y,p.x)*4.0),12.0);
        vec3 metal=vec3(0.14,0.065,0.045)+u_shine*vec3(0.85,0.42,0.27)*(spec+0.12*fine);
        rgb=mix(rgb,metal,mask);
    }
    color=vec4(rgb,1.0);
}
`,
    defaults: [0.35, 0.9, 1.2, 0.65, 1.0, 0.2].map(u_shine => ({ u_shine })),
    regression: ['20.0);', '2.0);'],
  },
  {
    id: 'T06', parameters: [
      { name: 'u_wave', min: 0.01, max: 0.16, value: 0.055, label: 'Wave slopes' },
      { name: 'u_sun', min: 0.05, max: 0.25, value: 0.14, label: 'Sun radius' },
    ],
    body: `
void main() {
    vec2 p=(gl_FragCoord.xy-0.5*u_resolution)/u_resolution.y;
    vec3 sky=vec3(0.66,0.30,0.25)+vec3(0.18,0.05,0.0)*exp(-dot(p-vec2(0.0,0.1),p-vec2(0.0,0.1))*2.0);
    float sunDistance=length(p-vec2(0.0,0.27));
    sky+=vec3(1.0,0.93,0.65)*(1.0-smoothstep(u_sun-0.005,u_sun+0.005,sunDistance));
    sky+=vec3(0.50,0.14,0.01)*exp(-max(sunDistance-u_sun,0.0)/0.065);
    vec3 rgb=sky;
    if(p.y<0.01) {
        float distanceToHorizon=1.0/max(0.02,0.01-p.y);
        vec2 q=vec2(p.x*distanceToHorizon,distanceToHorizon);
        float waves=sin(q.y*2.9+sin(q.x*3.0))+0.4*sin(q.y*6.0-q.x*4.0);
        float offset=(p.x+u_wave*0.6*sin(q.y*1.8))/max(0.025,0.16*(-p.y+0.05));
        float strip=exp(-(offset*offset));
        float glints=pow(max(0.0,0.5+0.5*sin(q.y*3.0+waves*u_wave*12.0)),18.0);
        rgb=vec3(0.40,0.26,0.23)+0.06*waves;
        rgb+=vec3(1.0,0.74,0.57)*strip*glints*1.7;
        rgb=mix(rgb,sky,smoothstep(-0.08,0.01,p.y)*0.55);
    }
    color=vec4(rgb,1.0);
}
`,
    defaults: [0.025, 0.055, 0.10, 0.075, 0.045, 0.14].map(u_wave => ({ u_wave })),
    regression: ['18.0);', '2.0);'],
  },
];

for (const shader of shaders) {
  const directory = join(root, shader.id);
  await mkdir(directory, { recursive: true });
  await writeFile(join(directory, 'seed.frag'), header(shader.parameters) + shader.body, { flag: 'wx' });
  const variants = shader.defaults.map((uniforms, index) => ({
    uniforms,
    changes: index === 5 ? [{ from: shader.regression[0], to: shader.regression[1] }] : [],
  }));
  await writeFile(join(directory, 'variants.json'), JSON.stringify(variants, null, 2) + '\n', { flag: 'wx' });
}
console.log(JSON.stringify({ seeds: shaders.map(shader => shader.id), source: 'constructed-procedural-GLSL' }));
