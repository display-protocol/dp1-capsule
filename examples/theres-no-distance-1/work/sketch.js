p5.disableFriendlyErrors = true;

let rez = 320;

let lineLength;
let lineThickness;
let lineDepth;
let lineSpacing;

let stillLifeDim = rez;
let stillLifeDepthNeg = 0.0;
let stillLifeDepthPos = 0.0;
let depthScalar = 1.0;
let lenthScalar = 1.3;

let stillLife;
let stillLifeBlur;

let backgroundColor = 0;

let minCubes = 3;
let maxCubes = 12;
let minCubeDia = 30;
let maxCubeDia = 70;
let cubeScalar = 1.0;

// Features
let backgroundParam = "";
let numCubes = -1;
let displayRed = true;
let displayGreen = true;
let displayBlue = true;

let viewDiagram = false;
let pauseCubes = false;
let cubeSpeed = 0.0;
let rotateCounter = 0;

/* START UTILS */

let gl;
let stillLifePO2 = 2;
let ext;

let pgl;

let viewMatrix;
let modelViewMatrix;
let dispMapTexture;

const vertCode = `precision highp float;
uniform mat4 uViewMatrix;
uniform mat4 uProjectionMatrix;
uniform mat4 uModelViewMatrix;
uniform mat4 uModelViewProjectionMatrix;
uniform vec2 canvasSize;
uniform float stillLifeDim;
uniform float stillLifePO2;
uniform float stillLifeDepthNeg;
uniform float stillLifeDepthPos;
uniform float depthScalar;
uniform float lineThickness;
uniform float lineDepth;
uniform float lineLength;
uniform vec3 color;
uniform sampler2D displacementMap;
attribute vec3 coordinates;

float map(float value, float min1, float max1, float min2, float max2)
{
    return min2 + (value - min1) * (max2 - min2) / (max1 - min1);
}

struct Line
{
    vec3 start;
    vec3 end;
};

Line getLineFromDisplacementMap(int x_origin, int y_origin)
{
    Line l;

    vec2 imageCoords = vec2(x_origin, y_origin);
    imageCoords.x += .5;
    imageCoords.y += .5;

    vec2 samplePos = imageCoords.xy/stillLifePO2;
    //vec3 dispMapColor = rgb2hsv(texture2D(displacementMap, samplePos).xyz);
    vec4 dispMapColor = texture2D(displacementMap, samplePos);
    float maxEdge = max(canvasSize.x, canvasSize.y);

    float x = float(x_origin);
    float y = float(y_origin);
    float z = map(dot(dispMapColor.xyz, color), 0.0, 1.0, -stillLifeDepthNeg, stillLifeDepthPos);

    l.start.x = map(x, 0.0, stillLifeDim, -maxEdge / 2.0, maxEdge / 2.0);
    l.start.y = map(y, 0.0, stillLifeDim, -maxEdge / 2.0, maxEdge / 2.0);
    l.start.z = -z;

    l.end.x = map(x, 0.0, stillLifeDim, -maxEdge / 2.0, maxEdge / 2.0) - lineDepth;
    l.end.y = map(y, 0.0, stillLifeDim, -maxEdge / 2.0, maxEdge / 2.0) - lineDepth;
    l.end.z = -lineLength - z;

    return l;
}

void main()
{
    bool lineStart = false;
    if (coordinates.y == 0.0) lineStart = true;
    if (coordinates.y == 1.0) lineStart = true;
    if (coordinates.y == 2.0) lineStart = false;
    if (coordinates.y == 3.0) lineStart = true;
    if (coordinates.y == 4.0) lineStart = false;
    if (coordinates.y == 5.0) lineStart = false;

    int imageCoordsY = int(floor(coordinates.x / stillLifeDim));
    int imageCoordsX = int(floor(coordinates.x - float(imageCoordsY) * stillLifeDim));

    Line l = getLineFromDisplacementMap(imageCoordsX, imageCoordsY);

    vec3 vertexPosition;
    if (lineStart) {
        vertexPosition = l.start;
    } else {
        vertexPosition = l.end;
    }

    vec4 finalPos = vec4(vertexPosition, 1.0);
    finalPos = uModelViewProjectionMatrix * finalPos;

    vec4 projLineStart = uModelViewProjectionMatrix * vec4(l.start, 1.0);
    vec4 projLineEnd = uModelViewProjectionMatrix * vec4(l.end, 1.0);
    vec2 lineDirection = projLineStart.xy - projLineEnd.xy;
    lineDirection = vec2(-lineDirection.y, lineDirection.x);
    float maxEdge = max(canvasSize.x, canvasSize.y);
    lineDirection /= canvasSize / maxEdge;
    vec2 displacement = normalize(lineDirection);
    displacement /= canvasSize;
    displacement *= lineThickness;

    if (coordinates.y == 0.0) finalPos.xy -= displacement;
    if (coordinates.y == 2.0) finalPos.xy -= displacement;
    if (coordinates.y == 5.0) finalPos.xy -= displacement;
    if (coordinates.y == 1.0) finalPos.xy += displacement;
    if (coordinates.y == 3.0) finalPos.xy += displacement;
    if (coordinates.y == 4.0) finalPos.xy += displacement;

    finalPos.z = 0.0;

    gl_Position = finalPos;
}
`;

const fragCode = `precision highp float;
uniform vec3 color;
uniform vec3 displayColor;

void main() {
    gl_FragColor = vec4(displayColor, 1.0);
}
`;

let triangle_buffer;
let numTris;

function setupGeometry() {
  let triangles = [];
  numTris = 0;
  for (let y = 0; y < rez; y += 2) {
    for (let x = 0; x < rez; x += 2) {
      triangles.push(x + y * stillLifeDim, 0, 0);
      triangles.push(x + y * stillLifeDim, 1, 0);
      triangles.push(x + y * stillLifeDim, 2, 0);
      triangles.push(x + y * stillLifeDim, 3, 0);
      triangles.push(x + y * stillLifeDim, 4, 0);
      triangles.push(x + y * stillLifeDim, 5, 0);
      numTris += 6;
    }
  }
  triangle_buffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, triangle_buffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(triangles), gl.STATIC_DRAW);
  gl.bindBuffer(gl.ARRAY_BUFFER, null);
}

let shaderProgram;

function setupShaders() {
  let vertShader = gl.createShader(gl.VERTEX_SHADER);
  gl.shaderSource(vertShader, vertCode);
  gl.compileShader(vertShader);

  const vertError = gl.getShaderInfoLog(vertShader);
  if (vertError.length > 0) {
    throw vertError;
  }

  let fragShader = gl.createShader(gl.FRAGMENT_SHADER);
  gl.shaderSource(fragShader, fragCode);
  gl.compileShader(fragShader);

  const fragError = gl.getShaderInfoLog(fragShader);
  if (fragError.length > 0) {
    throw fragError;
  }

  shaderProgram = gl.createProgram();
  gl.attachShader(shaderProgram, vertShader);
  gl.attachShader(shaderProgram, fragShader);
  gl.linkProgram(shaderProgram);
}

function createDisplacementMapTexture() {
  dispMapTexture = gl.createTexture();
  while (stillLifePO2 < stillLifeDim) {
    stillLifePO2 *= 2;
  }
}

function transferDisplacementMap() {
  let img = stillLifeBlur.get();
  img.loadPixels();

  let uvTexture = Array(stillLifePO2 * stillLifePO2 * 4).fill(255);

  const randomStuff = new Uint8Array(uvTexture);
  for (let x = 0; x < stillLifeDim; ++x) {
    for (let y = 0; y < stillLifeDim; ++y) {
      const index1 = 4 * (y * stillLifePO2 + x);
      const index2 = 4 * (y * stillLifeDim + x);
      randomStuff[index1 + 0] = img.pixels[index2 + 0];
      randomStuff[index1 + 1] = img.pixels[index2 + 1];
      randomStuff[index1 + 2] = img.pixels[index2 + 2];
    }
  }

  gl.bindTexture(gl.TEXTURE_2D, dispMapTexture);
  gl.texImage2D(
    gl.TEXTURE_2D,
    0,
    gl.RGBA,
    stillLifePO2,
    stillLifePO2,
    0,
    gl.RGBA,
    gl.UNSIGNED_BYTE,
    randomStuff
  );
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
  gl.bindTexture(gl.TEXTURE_2D, null);

  gl.activeTexture(gl.TEXTURE2);
  gl.bindTexture(gl.TEXTURE_2D, dispMapTexture);

  const displaceMapLocation = gl.getUniformLocation(
    shaderProgram,
    "displacementMap"
  );
  gl.uniform1i(displaceMapLocation, 2);
}

function setMatrices() {
  const viewMatrix = this._renderer._curCamera.cameraMatrix;
  const projectionMatrix = this._renderer.uPMatrix;
  const modelViewMatrix = this._renderer.uMVMatrix;
  const modelViewProjectionMatrix = modelViewMatrix.copy();
  modelViewProjectionMatrix.mult(projectionMatrix);

  gl.uniformMatrix4fv(
    gl.getUniformLocation(shaderProgram, "uViewMatrix"),
    false,
    viewMatrix.mat4
  );
  gl.uniformMatrix4fv(
    gl.getUniformLocation(shaderProgram, "uProjectionMatrix"),
    false,
    projectionMatrix.mat4
  );
  gl.uniformMatrix4fv(
    gl.getUniformLocation(shaderProgram, "uModelViewMatrix"),
    false,
    modelViewMatrix.mat4
  );
  gl.uniformMatrix4fv(
    gl.getUniformLocation(shaderProgram, "uModelViewProjectionMatrix"),
    false,
    modelViewProjectionMatrix.mat4
  );
}

function setUniforms() {
  gl.uniform2f(
    gl.getUniformLocation(shaderProgram, "canvasSize"),
    width,
    height
  );
  gl.uniform1f(
    gl.getUniformLocation(shaderProgram, "stillLifeDim"),
    stillLifeDim
  );
  gl.uniform1f(
    gl.getUniformLocation(shaderProgram, "stillLifePO2"),
    stillLifePO2
  );
  gl.uniform1f(
    gl.getUniformLocation(shaderProgram, "depthScalar"),
    depthScalar
  );
  gl.uniform1f(
    gl.getUniformLocation(shaderProgram, "stillLifeDepthNeg"),
    stillLifeDepthNeg * depthScalar
  );
    gl.uniform1f(
    gl.getUniformLocation(shaderProgram, "stillLifeDepthPos"),
    stillLifeDepthPos * depthScalar
  );
  gl.uniform1f(
    gl.getUniformLocation(shaderProgram, "lineThickness"),
    lineThickness
  );
  gl.uniform1f(gl.getUniformLocation(shaderProgram, "lineLength"), lineLength);
  gl.uniform1f(gl.getUniformLocation(shaderProgram, "lineDepth"), lineDepth);
}

function beginLinesShader() {
  gl.useProgram(shaderProgram);
  gl.bindBuffer(gl.ARRAY_BUFFER, triangle_buffer);

  transferDisplacementMap();
  setUniforms();

  const coord = gl.getAttribLocation(shaderProgram, "coordinates");
  gl.vertexAttribPointer(coord, 3, gl.FLOAT, false, 0, 0);
  gl.enableVertexAttribArray(coord);

  gl.enable(gl.BLEND);
  gl.disable(gl.DEPTH_TEST);
  gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
  gl.viewport(0, 0, canvas.width, canvas.height);
}

function endLinesShader() {
  gl.useProgram(null);
}

/* END UTILS */

function renderStillLife() {
  beginLinesShader();

  setMatrices();
  
  let ccc = (255.0 - backgroundColor) / 255.0;
  
  gl.uniform3f(gl.getUniformLocation(shaderProgram, "color"), 1, 0, 0);
  gl.uniform3f(gl.getUniformLocation(shaderProgram, "displayColor"), ccc, ccc, ccc);
  if (displayRed) {
    gl.drawArrays(gl.TRIANGLES, 0, numTris);
  }

  translate(lineSpacing, 0.0, 0.1);
  setMatrices();
  gl.uniform3f(gl.getUniformLocation(shaderProgram, "color"), 0, 1, 0);
  gl.uniform3f(gl.getUniformLocation(shaderProgram, "displayColor"), ccc, ccc, ccc);
  if (displayGreen) {
    gl.drawArrays(gl.TRIANGLES, 0, numTris);
  }
  
  translate(lineSpacing, 0.0, 0.1);
  setMatrices();
  gl.uniform3f(gl.getUniformLocation(shaderProgram, "color"), 0, 0, 1);
  gl.uniform3f(gl.getUniformLocation(shaderProgram, "displayColor"), ccc, ccc, ccc);
  if (displayBlue) {
    gl.drawArrays(gl.TRIANGLES, 0, numTris);
  }
  endLinesShader();
}

function setupTransform() {
  ortho();
  scale(1.45);
  rotateY(0.33);
  rotateX(-0.33);
}

class Cube {
  constructor(num) {
    this.x = map($fx.rand(), 0, 1, -stillLifeBlur.width/2, stillLifeBlur.width/2);
    this.y = map($fx.rand(), 0, 1, -stillLifeBlur.height/2, stillLifeBlur.height/2);
    if (num === 0) {
      this.x = map($fx.rand(), 0, 1, -stillLifeBlur.width/4, stillLifeBlur.width/4);
      this.y = 0.0;
    }
    this.rx = $fx.rand() * TWO_PI;
    this.ry = $fx.rand() * TWO_PI;
    this.dia = map($fx.rand(), 0, 1, minCubeDia, maxCubeDia) * cubeScalar;
  }
}

let allCubes = [];

function setup() {
  let c = createCanvas(windowWidth, windowHeight, WEBGL, document.getElementById("nodistance"));
  
  gl = c.GL;
  
  setupGeometry();
  setupShaders();
  createDisplacementMapTexture();
  
  stillLife = createGraphics(stillLifeDim, stillLifeDim, WEBGL);
  pgl = stillLife._renderer.GL;
  pgl.disable(gl.DEPTH_TEST);
  
  stillLifeBlur = createGraphics(stillLifeDim, stillLifeDim, WEBGL);
  stillLifeBlur.imageMode(CENTER);
  
  generateStillLife();
  stillLifeDepthNeg = 60;
  stillLifeDepthPos = 60;
}

function generateStillLife() {
  if ($fx.rand() < 0.1) {
    backgroundColor = 229;
    backgroundParam = "Day";
  } else {
    backgroundColor = 0;
    backgroundParam = "Night";
  }
  numCubes = int(map($fx.rand(), 0, 1, minCubes, maxCubes));
  cubeScalar = map(numCubes, minCubes, maxCubes, 1.5, 1.0);
  for (let i = 0; i < numCubes; i++) {
    allCubes[i] = new Cube(i);
  }
  displayRed = true;
  if ($fx.rand() > 0.5) {
    displayGreen = true;
  } else {
    displayGreen = false;
  }
  if ($fx.rand() > 0.5) {
    displayBlue = true;
  } else {
    displayBlue = false;
  }
  
  $fx.features({
  "# Cubes": int(numCubes),
  "Red": displayRed,
  "Green": displayGreen,
  "Blue": displayBlue,
  "Background" : backgroundParam
  })
}

function draw() {

  let scaleFactor = max(width, height) / 1200.0;
  
  lineThickness = 2.0 * scaleFactor;
  lineDepth = 2 * scaleFactor;
  lineLength = 8.0 * lenthScalar * scaleFactor;
  lineSpacing = 2.0 * scaleFactor;
  depthScalar = map(width, 600, 3840, 0.8, 2.0);
  
  if (!pauseCubes) {
    cubeSpeed = rotateCounter / 1000.0;
    rotateCounter++;
  }

  stillLife.background(0);
  
  for (let i = 0; i < numCubes; i++) {
    let x = allCubes[i].x;
    let y = allCubes[i].y;
    let rx = allCubes[i].rx;
    let ry = allCubes[i].ry;
    let rd = allCubes[i].dia;
    rgb(x, y, -30, rx, ry, rd, stillLife);
  }

  stillLifeBlur.tint(255, 51);
  stillLifeBlur.image(stillLife, 0, 0);
  
  background(backgroundColor);
  
  push();
  setupTransform();
  renderStillLife();
  pop();
  
  if (viewDiagram) {
    push();
    fill(255, 255, 255);
    rect(-100, -100, 200, 200);
    image(stillLifeBlur, -100, -100, 200, 200);
    pop();
  }
}

function keyPressed() {
  if (key == 'd' || key == 'D') {
    viewDiagram = !viewDiagram;
  } else if (key == ' ') {
    backgroundColor -= 26;
    if (backgroundColor === int(125)) {
      backgroundColor = 99;
    }
    if (backgroundColor <= 0) {
      backgroundColor = 229;
    }
  } else if (key == 'g' || key == 'G') {
    generateStillLife();
  } else if (key == 'p' || key == 'P') {
    pauseCubes = !pauseCubes;
  }
}

function windowResized() {
  resizeCanvas(windowWidth, windowHeight);
}

function rgb(x, y, z, rx, ry, sc, p) {
  
  p.noStroke();
  p.push(); 
  p.translate(x, y, z); 
  
  let xo = map(mouseX, 0, width, 0, TWO_PI);
  let yo = map(mouseY, 0, height, 0, TWO_PI);
  
  p.rotateX(rx + xo - cubeSpeed);
  p.rotateY(ry + yo + cubeSpeed);
  
  p.scale(sc);
  
  p.beginShape(QUADS);

  p.fill(0, 255, 255); 
  p.vertex(-1, 1, 1);
  p.fill(255, 255, 255); 
  p.vertex( 1, 1, 1);
  p.fill(255, 0, 255); 
  p.vertex( 1, -1, 1);
  p.fill(0, 0, 255); 
  p.vertex(-1, -1, 1);

  p.fill(255, 255, 255); 
  p.vertex( 1, 1, 1);
  p.fill(255, 255, 0); 
  p.vertex( 1, 1, -1);
  p.fill(255, 0, 0); 
  p.vertex( 1, -1, -1);
  p.fill(255, 0, 255); 
  p.vertex( 1, -1, 1);

  p.fill(255, 255, 0); 
  p.vertex( 1, 1, -1);
  p.fill(0, 255, 0); 
  p.vertex(-1, 1, -1);
  p.fill(0, 0, 0); 
  p.vertex(-1, -1, -1);
  p.fill(255, 0, 0); 
  p.vertex( 1, -1, -1);

  p.fill(0, 255, 0); 
  p.vertex(-1, 1, -1);
  p.fill(0, 255, 255); 
  p.vertex(-1, 1, 1);
  p.fill(0, 0, 255); 
  p.vertex(-1, -1, 1);
  p.fill(0, 0, 0); 
  p.vertex(-1, -1, -1);

  p.fill(0, 255, 0); 
  p.vertex(-1, 1, -1);
  p.fill(255, 255, 0); 
  p.vertex( 1, 1, -1);
  p.fill(255, 255, 255); 
  p.vertex( 1, 1, 1);
  p.fill(0, 255, 255); 
  p.vertex(-1, 1, 1);

  p.fill(0, 0, 0); 
  p.vertex(-1, -1, -1);
  p.fill(255, 0, 0); 
  p.vertex( 1, -1, -1);
  p.fill(255, 0, 255); 
  p.vertex( 1, -1, 1);
  p.fill(0, 0, 255); 
  p.vertex(-1, -1, 1);

  p.endShape();
  p.pop();
}