
        const canvas = document.getElementById('realistic-world-canvas');
        const ctx = canvas.getContext('2d');

        const WORLD_WIDTH = 7200;
        const WORLD_HEIGHT = 1200;

        let isNightMode = true;
        let nightMix = 1.0;
        let targetScrollProgress = 0;
        let smoothScrollProgress = 0;
        let animTime = 0;

        let isFaultActive = false;
        let isRerouted = false;
        let activeScenarioId = 1;
        let lightningFlashAlpha = 0;
        let isDrawerOpen = false;
        let autoCycleTimer = null;

        const cypherQueries = [
            "MATCH path = (t3:Tower {id:'T3'})-[:SPAN|TERMINATES_AT*1..7]->(sub:Substation)\nWHERE sub.city = 'MetroGrid'\nRETURN path, sub.capacity_mw;",
            "MATCH (:Tower)-[s:SPAN {id:'TX_882', circuit:'UPPER_PRIMARY'}]->(t:Tower)\nMATCH (t)-[:SPAN|TERMINATES_AT*1..7]->(:Substation)-[:DISTRIBUTES_TO]->(load:Consumer)\nWHERE load.priority_tier = 3\nRETURN DISTINCT load.breaker_id, load.mw_demand;",
            "MATCH (sub:Substation {id:'SUB_METRO'})<-[:CONNECTED]-(b:BESS)\nWHERE b.soc_pct > 40\nRETURN b.id, b.max_discharge_mw;"
        ];
        let activeCypherIndex = 0;

        const scrollStageWindows = [
            { id: 'scroll-card-0', start: 0.05, peakStart: 0.13, peakEnd: 0.26, end: 0.35, curOpacity: 0, curPhase: -1 },
            { id: 'scroll-card-1', start: 0.30, peakStart: 0.38, peakEnd: 0.53, end: 0.62, curOpacity: 0, curPhase: -1 },
            { id: 'scroll-card-2', start: 0.57, peakStart: 0.65, peakEnd: 0.78, end: 0.86, curOpacity: 0, curPhase: -1 },
            { id: 'scroll-card-3', start: 0.81, peakStart: 0.89, peakEnd: 1.00, end: 1.12, curOpacity: 0, curPhase: -1 }
        ];
        let smoothHeroAlpha = 1.0;

        const towers = [
            { id: 'T1', x: 520,  baseY: 855, h: 270 },
            { id: 'T2', x: 1080, baseY: 875, h: 265 },
            { id: 'T3', x: 1660, baseY: 890, h: 275 },
            { id: 'T4', x: 2260, baseY: 912, h: 260 },
            { id: 'T5', x: 2860, baseY: 925, h: 270 },
            { id: 'T6', x: 3480, baseY: 940, h: 265 },
            { id: 'T7', x: 4090, baseY: 952, h: 275 },
            { id: 'T8', x: 4690, baseY: 965, h: 265 },
            { id: 'T9', x: 5190, baseY: 978, h: 255 }
        ];
        const substationPort = { x: 5545, y: 865 };

        const waterfallUpperStreams = Array.from({ length: 19 }, (_, i) => {
            const u = (i + 0.5) / 19;
            const jitter = Math.sin(i * 2.37) * 0.022;
            const normU = Math.max(0.04, Math.min(0.96, u + jitter));
            return {
                u: normU,
                topDropOffset: Math.abs(Math.sin(i * 1.9 + 0.4)) * 14,
                speed: 68 + ((i * 29) % 100),
                dashA: 14 + ((i * 13) % 24),
                dashB: 16 + ((i * 17) % 28),
                phase: (i * 47) % 220,
                width: 1.2 + ((i * 7) % 5) * 0.42,
                swayAmp: 1.8 + (i % 4) * 0.9,
                swayFreq: 0.9 + (i % 5) * 0.35
            };
        });

        const waterfallOutflowThreads = Array.from({ length: 22 }, (_, i) => {
            const u = 0.04 + (i / 21) * 0.92;
            return {
                u,
                speed: 44 + ((i * 23) % 64),
                dashA: 12 + ((i * 11) % 20),
                dashB: 18 + ((i * 19) % 26),
                phase: (i * 53) % 180,
                width: 1.15 + ((i * 5) % 4) * 0.38
            };
        });

        const stars = Array.from({ length: 160 }, (_, i) => ({
            x: (i * 173 + 97) % 7000,
            y: (i * 89 + 23) % 540,
            r: (i % 3 === 0) ? 1.5 : 1.0,
            alpha: 0.35 + ((i * 31) % 60) / 100
        }));

        const clouds = [
            {
                x: 420, y: 185, speed: 14, scale: 1.05,
                puffs: [
                    { dx: -110, dy: 14, rx: 115, ry: 36 },
                    { dx: -45,  dy: -10, rx: 135, ry: 48 },
                    { dx: 35,   dy: -16, rx: 150, ry: 54 },
                    { dx: 115,  dy: 6,   rx: 125, ry: 40 },
                    { dx: 0,    dy: 18,  rx: 175, ry: 38 }
                ]
            },
            {
                x: 1220, y: 140, speed: 18, scale: 1.15,
                puffs: [
                    { dx: -130, dy: 16, rx: 125, ry: 38 },
                    { dx: -55,  dy: -14, rx: 155, ry: 56 },
                    { dx: 30,   dy: -22, rx: 165, ry: 60 },
                    { dx: 110,  dy: -4,  rx: 140, ry: 48 },
                    { dx: 170,  dy: 16,  rx: 110, ry: 34 },
                    { dx: 15,   dy: 20,  rx: 195, ry: 42 }
                ]
            },
            {
                x: 2180, y: 175, speed: 12, scale: 1.0,
                puffs: [
                    { dx: -105, dy: 12, rx: 110, ry: 35 },
                    { dx: -30,  dy: -15, rx: 140, ry: 50 },
                    { dx: 55,   dy: -8,  rx: 135, ry: 46 },
                    { dx: 125,  dy: 14,  rx: 105, ry: 34 },
                    { dx: 10,   dy: 18,  rx: 165, ry: 36 }
                ]
            },
            {
                x: 3140, y: 150, speed: 16, scale: 1.2,
                puffs: [
                    { dx: -140, dy: 14, rx: 130, ry: 40 },
                    { dx: -60,  dy: -18, rx: 160, ry: 58 },
                    { dx: 25,   dy: -24, rx: 170, ry: 62 },
                    { dx: 115,  dy: -6,  rx: 145, ry: 50 },
                    { dx: 180,  dy: 14,  rx: 115, ry: 36 },
                    { dx: 15,   dy: 22,  rx: 210, ry: 44 }
                ]
            },
            {
                x: 4200, y: 185, speed: 15, scale: 0.98,
                puffs: [
                    { dx: -95, dy: 12, rx: 105, ry: 34 },
                    { dx: -20, dy: -14, rx: 135, ry: 48 },
                    { dx: 60,  dy: -10, rx: 130, ry: 45 },
                    { dx: 125, dy: 12,  rx: 100, ry: 32 },
                    { dx: 15,  dy: 16,  rx: 155, ry: 35 }
                ]
            },
            {
                x: 5280, y: 160, speed: 13, scale: 1.08,
                puffs: [
                    { dx: -115, dy: 14, rx: 120, ry: 38 },
                    { dx: -35,  dy: -16, rx: 145, ry: 52 },
                    { dx: 50,   dy: -12, rx: 140, ry: 48 },
                    { dx: 130,  dy: 10,  rx: 115, ry: 36 },
                    { dx: 10,   dy: 18,  rx: 175, ry: 38 }
                ]
            }
        ];

        const wildernessBoulders = [
            { x: 95,   yOff: 18,  r: 22, aspect: 0.62, moss: true },
            { x: 525,  yOff: 85,  r: 26, aspect: 0.56, moss: true },
            { x: 640,  yOff: 95,  r: 27, aspect: 0.58, moss: true },
            { x: 890,  yOff: 115, r: 24, aspect: 0.60, moss: true },
            { x: 1210, yOff: 95,  r: 30, aspect: 0.52, moss: false },
            { x: 1490, yOff: 110, r: 24, aspect: 0.60, moss: true },
            { x: 1840, yOff: 92,  r: 26, aspect: 0.56, moss: true },
            { x: 2180, yOff: 105, r: 20, aspect: 0.65, moss: false },
            { x: 2460, yOff: 96,  r: 23, aspect: 0.58, moss: true }
        ];

        const valleyHouses = [
            { x: 2880, yOff: 32, w: 38, h: 24, roofH: 18, floors: 1, windows: 2, chimney: true,  style: 'cabin' },
            { x: 3190, yOff: 68, w: 42, h: 26, roofH: 20, floors: 1, windows: 2, chimney: true,  style: 'cabin' },
            { x: 3420, yOff: 28, w: 44, h: 28, roofH: 19, floors: 1, windows: 3, chimney: false, style: 'cabin' },
            { x: 3640, yOff: 62, w: 48, h: 30, roofH: 21, floors: 1, windows: 3, chimney: true,  style: 'cabin' },
            { x: 3840, yOff: 26, w: 54, h: 36, roofH: 20, floors: 2, windows: 3, chimney: true,  style: 'cottage' },
            { x: 3965, yOff: 64, w: 48, h: 32, roofH: 18, floors: 1, windows: 3, chimney: false, style: 'cottage' },
            { x: 4160, yOff: 30, w: 58, h: 40, roofH: 22, floors: 2, windows: 4, chimney: true,  style: 'cottage' },
            { x: 4275, yOff: 60, w: 52, h: 36, roofH: 19, floors: 2, windows: 3, chimney: false, style: 'cottage' },
            { x: 4410, yOff: 25, w: 62, h: 42, roofH: 22, floors: 2, windows: 4, chimney: true,  style: 'cottage' },
            { x: 4565, yOff: 52, w: 66, h: 46, roofH: 20, floors: 2, windows: 4, chimney: false, style: 'suburban' },
            { x: 4660, yOff: 22, w: 58, h: 44, roofH: 18, floors: 2, windows: 4, chimney: true,  style: 'suburban' },
            { x: 4785, yOff: 48, w: 72, h: 52, roofH: 21, floors: 2, windows: 5, chimney: false, style: 'suburban' },
            { x: 4890, yOff: 20, w: 64, h: 48, roofH: 19, floors: 2, windows: 4, chimney: false, style: 'suburban' },
            { x: 4995, yOff: 44, w: 76, h: 58, roofH: 20, floors: 3, windows: 5, chimney: true,  style: 'suburban' },
            { x: 5105, yOff: 22, w: 68, h: 54, roofH: 18, floors: 3, windows: 4, chimney: false, style: 'suburban' },
            { x: 5240, yOff: 38, w: 80, h: 64, roofH: 18, floors: 3, windows: 5, chimney: false, style: 'suburban' },
            { x: 5345, yOff: 24, w: 74, h: 60, roofH: 16, floors: 3, windows: 5, chimney: false, style: 'suburban' }
        ];

        const bgCityBuildings = [
            { x: 5665, w: 95,  h: 270, floors: 20, cols: 5 },
            { x: 5790, w: 110, h: 390, floors: 28, cols: 6 },
            { x: 5935, w: 105, h: 460, floors: 34, cols: 5 },
            { x: 6085, w: 120, h: 380, floors: 26, cols: 6 },
            { x: 6230, w: 115, h: 510, floors: 36, cols: 6 },
            { x: 6380, w: 100, h: 440, floors: 30, cols: 5 },
            { x: 6510, w: 125, h: 530, floors: 38, cols: 6 },
            { x: 6665, w: 110, h: 490, floors: 35, cols: 5 },
            { x: 6805, w: 115, h: 420, floors: 30, cols: 6 },
            { x: 6945, w: 105, h: 470, floors: 33, cols: 5 },
            { x: 7075, w: 95,  h: 340, floors: 24, cols: 5 }
        ];

        const cityBuildings = [
            { x: 5710, w: 92,  h: 295, floors: 21, cols: 5, spire: false, crown: true },
            { x: 5812, w: 105, h: 375, floors: 27, cols: 6, spire: true,  crown: false },
            { x: 5928, w: 118, h: 465, floors: 33, cols: 6, spire: true,  crown: true },
            { x: 6056, w: 96,  h: 340, floors: 24, cols: 5, spire: false, crown: false },
            { x: 6162, w: 132, h: 555, floors: 40, cols: 7, spire: true,  crown: true },
            { x: 6304, w: 185, h: 415, floors: 27, cols: 9, isHospital: true, crown: true },
            { x: 6500, w: 112, h: 485, floors: 35, cols: 6, spire: false, crown: true },
            { x: 6622, w: 142, h: 610, floors: 44, cols: 7, spire: true,  crown: true },
            { x: 6774, w: 104, h: 440, floors: 31, cols: 6, spire: false, crown: false },
            { x: 6888, w: 124, h: 520, floors: 37, cols: 7, spire: true,  crown: true },
            { x: 7022, w: 98,  h: 390, floors: 28, cols: 5, spire: false, crown: true },
            { x: 7130, w: 86,  h: 310, floors: 22, cols: 5, spire: false, crown: false }
        ];

        function lerp(a, b, t) {
            return a + (b - a) * t;
        }

        function lerpRGB(dayRGB, nightRGB, t) {
            const r = Math.round(lerp(dayRGB[0], nightRGB[0], t));
            const g = Math.round(lerp(dayRGB[1], nightRGB[1], t));
            const b = Math.round(lerp(dayRGB[2], nightRGB[2], t));
            return `rgb(${r}, ${g}, ${b})`;
        }

        function lerpRGBA(dayRGB, nightRGB, t, alpha) {
            const r = Math.round(lerp(dayRGB[0], nightRGB[0], t));
            const g = Math.round(lerp(dayRGB[1], nightRGB[1], t));
            const b = Math.round(lerp(dayRGB[2], nightRGB[2], t));
            return `rgba(${r}, ${g}, ${b}, ${alpha})`;
        }

        function smoothstep(edge0, edge1, x) {
            const t = Math.max(0, Math.min(1, (x - edge0) / Math.max(0.0001, edge1 - edge0)));
            return t * t * (3 - 2 * t);
        }

        function getValleyGroundY(x) {
            const t = Math.min(1, Math.max(0, x / 5400));
            const cityFlatten = x > 5100 ? Math.max(0, 1 - (x - 5100) / 450) : 1;
            const macroMeander = Math.sin(x * 0.0022 + 0.4) * 19 + Math.cos(x * 0.0054 - 0.9) * 12;
            const mesoKnolls = Math.sin(x * 0.014 + 1.7) * 8.5 + Math.cos(x * 0.029 + 0.5) * 5.2;
            const microBumps = Math.sin(x * 0.068) * 2.8 + Math.cos(x * 0.135 + 1.1) * 1.6;
            return 846 + t * 138 + (macroMeander + mesoKnolls + microBumps) * cityFlatten;
        }

        function getExactRiverTopY(x) {
            if (x >= 5520) return 1025;
            const t = Math.max(0, Math.min(1, x / 5400));
            const mt = 1 - t;
            const baseCurve = (
                mt * mt * mt * 1052 +
                3 * mt * mt * t * 1016 +
                3 * mt * t * t * 1044 +
                t * t * t * 1025
            );
            const naturalBankMask = x > 5050 ? Math.max(0, 1 - (x - 5050) / 470) : 1;
            const wideBends = Math.sin(x * 0.0031 + 0.6) * 18 + Math.cos(x * 0.0078 - 1.2) * 11.5;
            const rockyInlets = Math.sin(x * 0.019 + 2.4) * 7.5 + Math.cos(x * 0.043 - 0.7) * 4.8;
            const bankJaggedness = Math.sin(x * 0.095 + 0.9) * 2.6 + Math.cos(x * 0.18 + 2.1) * 1.5;
            return baseCurve + (wideBends + rockyInlets + bankJaggedness) * naturalBankMask;
        }

        function getRiverShoreY(x) {
            return getExactRiverTopY(x) - 15;
        }

        const forestTrees = [];
        (function buildDenseRiverbankForest() {
            const depthRows = [0.02, 0.24, 0.48, 0.72, 0.92];
            depthRows.forEach((rowFrac, rowIdx) => {
                for (let x = 25; x < 5380; ) {
                    let step = 24 + rowIdx * 2;
                    if (x >= 2850 && x < 4200) step = 46 + rowIdx * 5;
                    else if (x >= 4200) step = 78 + rowIdx * 8;

                    const jitterX = ((x * 37 + rowIdx * 73) % 19) - 9;
                    const tx = x + jitterX;
                    x += step;

                    if (tx > 135 && tx < 495) continue;

                    const upperY = getValleyGroundY(tx) - (tx < 2600 ? 10 : 0);
                    const shoreY = getRiverShoreY(tx);
                    const rawGroundY = lerp(upperY, shoreY, rowFrac) + ((tx * 13 + rowIdx * 11) % 9) - 4;
                    const groundY = Math.min(rawGroundY, shoreY - 4);

                    const nearTowerPad = towers.some(tw => Math.abs(tx - tw.x) < 26 && Math.abs(groundY - tw.baseY) < 22);
                    if (nearTowerPad) continue;

                    const overlapsHouse = valleyHouses.some(h => {
                        const hy = getValleyGroundY(h.x + h.w * 0.5) + h.yOff;
                        return tx > h.x - 16 && tx < h.x + h.w + 16 && Math.abs(groundY - hy) < 42;
                    });
                    if (overlapsHouse) continue;

                    const seed = (Math.abs(Math.floor(tx)) * 29 + rowIdx * 43) % 41;
                    const wildernessBoost = tx < 2750 ? 16 : 4;
                    const treeH = 44 + wildernessBoost + rowIdx * 5 + (seed % 24);
                    const scale = (0.76 + rowFrac * 0.34) * (tx < 2750 ? 1.06 : 0.92);

                    forestTrees.push({
                        x: tx,
                        groundY,
                        treeH,
                        scale,
                        seed,
                        rowIdx
                    });
                }
            });
            forestTrees.sort((a, b) => a.groundY - b.groundY);
        })();

        function getRidgeHeight(x, seed, baseAlt, amp) {
            const n1 = Math.sin(x * 0.0014 + seed) * 0.52;
            const n2 = Math.sin(x * 0.0037 + seed * 2.1) * 0.28;
            const n3 = Math.sin(x * 0.0089 - seed * 1.4) * 0.14;
            const n4 = Math.cos(x * 0.019 + seed * 3.7) * 0.06;
            const cityTaper = x > 4800 ? Math.max(0.18, 1 - (x - 4800) / 2200) : 1;
            return baseAlt - (n1 + n2 + n3 + n4) * amp * cityTaper;
        }

        function resizeCanvas() {
            const dpr = Math.min(window.devicePixelRatio || 1, 2);
            canvas.width = Math.max(300, window.innerWidth * dpr);
            canvas.height = Math.max(200, window.innerHeight * dpr);
        }
        window.addEventListener('resize', resizeCanvas);
        resizeCanvas();

        function drawSkyAndAtmosphere(camX, viewWorldW) {
            const skyGrad = ctx.createLinearGradient(0, 0, 0, WORLD_HEIGHT);
            skyGrad.addColorStop(0.0, lerpRGB([28, 100, 178],  [3, 7, 18],   nightMix));
            skyGrad.addColorStop(0.45,lerpRGB([112, 178, 228], [10, 20, 42], nightMix));
            skyGrad.addColorStop(0.78,lerpRGB([248, 212, 164], [18, 33, 64], nightMix));
            skyGrad.addColorStop(1.0, lerpRGB([253, 230, 195], [15, 23, 42], nightMix));
            ctx.fillStyle = skyGrad;
            ctx.fillRect(camX - 200, 0, WORLD_WIDTH + 400, WORLD_HEIGHT);

            if (nightMix > 0.05) {
                ctx.save();
                stars.forEach(st => {
                    const sx = st.x + camX * 0.72;
                    if (sx < camX - 100 || sx > camX + 2800) return;
                    ctx.fillStyle = `rgba(226, 232, 240, ${st.alpha * nightMix})`;
                    ctx.beginPath();
                    ctx.arc(sx, st.y, st.r, 0, Math.PI * 2);
                    ctx.fill();
                });
                ctx.restore();
            }

            const safeViewW = Math.max(900, Number.isFinite(viewWorldW) ? viewWorldW : 1920);
            const skyArcY = Math.sin(smoothScrollProgress * Math.PI) * -34 + Math.sin(animTime * 0.45) * 4;

            if (nightMix < 0.98) {
                const sunScreenFrac = lerp(0.68, 0.28, smoothScrollProgress);
                const sunX = camX + safeViewW * sunScreenFrac + Math.cos(animTime * 0.35) * 6;
                const sunY = lerp(225, 760, nightMix) + skyArcY;
                const sunAlpha = 1 - nightMix;
                const sunGlow = ctx.createRadialGradient(sunX, sunY, 12, sunX, sunY, 320);
                sunGlow.addColorStop(0,   `rgba(255, 251, 235, ${0.95 * sunAlpha})`);
                sunGlow.addColorStop(0.25,`rgba(253, 224, 71, ${0.55 * sunAlpha})`);
                sunGlow.addColorStop(0.6, `rgba(251, 146, 60, ${0.22 * sunAlpha})`);
                sunGlow.addColorStop(1,   'rgba(251, 146, 60, 0)');
                ctx.fillStyle = sunGlow;
                ctx.beginPath();
                ctx.arc(sunX, sunY, 320, 0, Math.PI * 2);
                ctx.fill();

                ctx.fillStyle = `rgba(255, 253, 240, ${sunAlpha})`;
                ctx.beginPath();
                ctx.arc(sunX, sunY, 46, 0, Math.PI * 2);
                ctx.fill();
            }

            if (nightMix > 0.02) {
                const moonScreenFrac = lerp(0.74, 0.32, smoothScrollProgress);
                const moonX = camX + safeViewW * moonScreenFrac + Math.sin(animTime * 0.3) * 6;
                const moonY = lerp(720, 205, nightMix) + skyArcY;
                const mGlow = ctx.createRadialGradient(moonX, moonY, 10, moonX, moonY, 210);
                mGlow.addColorStop(0,   `rgba(186, 230, 253, ${0.42 * nightMix})`);
                mGlow.addColorStop(0.5, `rgba(56, 189, 248, ${0.12 * nightMix})`);
                mGlow.addColorStop(1,   'rgba(56, 189, 248, 0)');
                ctx.fillStyle = mGlow;
                ctx.beginPath();
                ctx.arc(moonX, moonY, 210, 0, Math.PI * 2);
                ctx.fill();

                ctx.fillStyle = `rgba(241, 245, 249, ${nightMix})`;
                ctx.beginPath();
                ctx.arc(moonX, moonY, 38, 0, Math.PI * 2);
                ctx.fill();

                ctx.fillStyle = `rgba(148, 163, 184, ${0.35 * nightMix})`;
                ctx.beginPath();
                ctx.arc(moonX - 12, moonY - 8, 9, 0, Math.PI * 2);
                ctx.arc(moonX + 14, moonY + 10, 7, 0, Math.PI * 2);
                ctx.arc(moonX + 4, moonY - 15, 5, 0, Math.PI * 2);
                ctx.fill();
            }

            clouds.forEach((c, idx) => {
                const driftX = (animTime * c.speed) % 6600;
                const baseCx = ((c.x + driftX) % 6600) - 200 + camX * 0.52;
                const baseCy = c.y + Math.sin(animTime * 0.35 + idx * 1.4) * 5;
                if (baseCx + 380 < camX - 250 || baseCx - 380 > camX + 2700) return;

                c.puffs.forEach((p, pIdx) => {
                    const px = baseCx + p.dx * c.scale + Math.sin(animTime * 0.55 + idx + pIdx) * 6;
                    const py = baseCy + p.dy * c.scale + Math.cos(animTime * 0.45 + pIdx) * 2.5;
                    const rx = p.rx * c.scale;
                    const ry = p.ry * c.scale;

                    ctx.save();
                    ctx.translate(px, py);
                    ctx.scale(rx, ry);
                    const puffGrad = ctx.createRadialGradient(-0.12, -0.18, 0.02, 0, 0, 1);
                    puffGrad.addColorStop(0, lerpRGBA([255, 255, 255], [71, 85, 105], nightMix, lerp(0.78, 0.24, nightMix)));
                    puffGrad.addColorStop(0.45, lerpRGBA([248, 250, 252], [51, 65, 85], nightMix, lerp(0.46, 0.14, nightMix)));
                    puffGrad.addColorStop(0.78, lerpRGBA([226, 232, 240], [30, 41, 59], nightMix, lerp(0.18, 0.05, nightMix)));
                    puffGrad.addColorStop(1, 'rgba(255, 255, 255, 0)');
                    ctx.fillStyle = puffGrad;
                    ctx.beginPath();
                    ctx.arc(0, 0, 1, 0, Math.PI * 2);
                    ctx.fill();
                    ctx.restore();
                });
            });
        }

        function drawMountainRange(camX, seed, baseAlt, amp, parallax, dayRGB, nightRGB, snowLine) {
            ctx.save();
            ctx.beginPath();
            const startX = Math.max(0, Math.floor((camX - 200) / 25) * 25);
            const endX = Math.min(WORLD_WIDTH, startX + 2900);

            ctx.moveTo(startX, WORLD_HEIGHT);
            for (let x = startX; x <= endX; x += 25) {
                const sampleX = x - camX * (1 - parallax);
                const y = getRidgeHeight(sampleX, seed, baseAlt, amp);
                ctx.lineTo(x, y);
            }
            ctx.lineTo(endX, WORLD_HEIGHT);
            ctx.closePath();

            const mGrad = ctx.createLinearGradient(0, baseAlt - amp, 0, WORLD_HEIGHT);
            mGrad.addColorStop(0, lerpRGB(dayRGB, nightRGB, nightMix));
            mGrad.addColorStop(1, lerpRGB([20, 32, 48], [4, 9, 20], nightMix));
            ctx.fillStyle = mGrad;
            ctx.fill();

            ctx.strokeStyle = snowLine
                ? lerpRGBA([255, 255, 255], [148, 163, 184], nightMix, lerp(0.65, 0.28, nightMix))
                : lerpRGBA([203, 213, 225], [51, 65, 85], nightMix, 0.25);
            ctx.lineWidth = snowLine ? 2.2 : 1.2;
            ctx.stroke();
            ctx.restore();
        }

        function drawValleyMist(camX, yPos, thickness) {
            const mistGrad = ctx.createLinearGradient(0, yPos - thickness * 1.35, 0, yPos + thickness * 1.35);
            mistGrad.addColorStop(0, 'rgba(148, 163, 184, 0)');
            mistGrad.addColorStop(0.28, lerpRGBA([241, 245, 249], [36, 66, 104], nightMix, lerp(0.45, 0.28, nightMix)));
            mistGrad.addColorStop(0.5, lerpRGBA([255, 255, 255], [52, 86, 128], nightMix, lerp(0.68, 0.44, nightMix)));
            mistGrad.addColorStop(0.72, lerpRGBA([241, 245, 249], [36, 66, 104], nightMix, lerp(0.45, 0.28, nightMix)));
            mistGrad.addColorStop(1, 'rgba(148, 163, 184, 0)');
            ctx.fillStyle = mistGrad;
            ctx.fillRect(camX - 100, yPos - thickness * 1.35, 2800, thickness * 2.7);
        }

        function drawRealisticSpruceTree(tx, groundY, treeH, scale, seed, sway, rowIdx = 2) {
            const trunkW = Math.max(2, 3.2 * scale);
            const trunkH = treeH * 0.22;

            ctx.fillStyle = `rgba(2, 6, 23, ${0.18 + rowIdx * 0.04})`;
            ctx.beginPath();
            ctx.ellipse(tx, groundY, 9 * scale, 2.6 * scale, 0, 0, Math.PI * 2);
            ctx.fill();

            ctx.fillStyle = lerpRGB([74, 53, 37], [18, 16, 22], nightMix);
            ctx.fillRect(tx - trunkW * 0.5, groundY - trunkH, trunkW, trunkH + 2);

            const depthBoost = (rowIdx - 2) * 3;
            const shadowFoliage = lerpRGB(
                [Math.max(12, 19 + depthBoost), Math.max(32, 48 + depthBoost), Math.max(24, 35 + depthBoost)],
                [Math.max(3, 5 + rowIdx), Math.max(10, 14 + rowIdx * 2), Math.max(18, 24 + rowIdx * 2)],
                nightMix
            );
            const litFoliage = lerpRGB(
                [Math.max(30, 46 + depthBoost), Math.max(75, 98 + depthBoost), Math.max(50, 66 + depthBoost)],
                [Math.max(10, 14 + rowIdx * 2), Math.max(28, 36 + rowIdx * 3), Math.max(42, 54 + rowIdx * 3)],
                nightMix
            );

            const tiers = 4;
            for (let layer = 0; layer < tiers; layer++) {
                const progress = layer / (tiers - 1);
                const tierBottomY = groundY - trunkH * 0.65 - layer * (treeH * 0.21);
                const tierTopY = tierBottomY - treeH * (0.34 - progress * 0.04);
                const halfSpan = (12 + (1 - progress) * 14) * scale;
                const layerSway = sway * (0.3 + progress * 0.7);
                const apexX = tx + layerSway;

                ctx.fillStyle = shadowFoliage;
                ctx.beginPath();
                ctx.moveTo(apexX, tierTopY);
                ctx.quadraticCurveTo(
                    tx + halfSpan * 0.55 + layerSway * 0.5,
                    tierBottomY - 4 * scale,
                    tx + halfSpan + layerSway * 0.3,
                    tierBottomY
                );
                ctx.lineTo(tx - halfSpan + layerSway * 0.3, tierBottomY);
                ctx.quadraticCurveTo(
                    tx - halfSpan * 0.55 + layerSway * 0.5,
                    tierBottomY - 4 * scale,
                    apexX,
                    tierTopY
                );
                ctx.closePath();
                ctx.fill();

                ctx.fillStyle = litFoliage;
                ctx.beginPath();
                ctx.moveTo(apexX, tierTopY);
                ctx.lineTo(tx + halfSpan * 0.1, tierBottomY + 1.5 * scale);
                ctx.lineTo(tx - halfSpan + layerSway * 0.3, tierBottomY);
                ctx.quadraticCurveTo(
                    tx - halfSpan * 0.55 + layerSway * 0.5,
                    tierBottomY - 4 * scale,
                    apexX,
                    tierTopY
                );
                ctx.closePath();
                ctx.fill();
            }
        }

        function drawAlpineHeadwatersWaterfall(camX) {
            const wfX = 275;
            if (camX > 1450) return;

            const topY = 534;
            const midLedgeY = 690;
            const poolY = 868;
            const riverJoinY = getExactRiverTopY(wfX + 95) + 28;

            const topHalfW = 44;
            const midHalfW = 60;
            const poolHalfW = 80;

            const massifGrad = ctx.createLinearGradient(wfX, 430, wfX, poolY + 10);
            massifGrad.addColorStop(0.0, lerpRGB([74, 108, 140], [16, 31, 56], nightMix));
            massifGrad.addColorStop(0.45, lerpRGB([52, 84, 108], [12, 23, 42], nightMix));
            massifGrad.addColorStop(1.0, lerpRGB([28, 52, 68],  [6, 13, 24],  nightMix));

            ctx.fillStyle = massifGrad;
            ctx.beginPath();
            ctx.moveTo(-80, 868);
            ctx.lineTo(-80, 620);
            ctx.quadraticCurveTo(25, 535, 118, 462);
            ctx.quadraticCurveTo(185, 505, wfX - topHalfW - 10, topY - 4);
            ctx.lineTo(wfX, topY + 8);
            ctx.lineTo(wfX + topHalfW + 10, topY - 6);
            ctx.quadraticCurveTo(385, 475, 458, 436);
            ctx.quadraticCurveTo(575, 488, 688, 514);
            ctx.quadraticCurveTo(790, 542, 915, 568);
            ctx.quadraticCurveTo(1065, 650, 1240, 795);
            ctx.lineTo(1240, 885);
            ctx.closePath();
            ctx.fill();

            const litFaceGrad = ctx.createLinearGradient(0, 440, 0, poolY);
            litFaceGrad.addColorStop(0, lerpRGBA([116, 152, 184], [26, 46, 78], nightMix, 0.75));
            litFaceGrad.addColorStop(1, lerpRGBA([45, 76, 94],    [9, 18, 32],  nightMix, 0.25));
            ctx.fillStyle = litFaceGrad;
            ctx.beginPath();
            ctx.moveTo(-80, 868);
            ctx.lineTo(-80, 620);
            ctx.quadraticCurveTo(25, 535, 118, 462);
            ctx.quadraticCurveTo(148, 645, 165, 868);
            ctx.closePath();
            ctx.fill();

            ctx.beginPath();
            ctx.moveTo(wfX + topHalfW + 10, topY - 6);
            ctx.quadraticCurveTo(385, 475, 458, 436);
            ctx.quadraticCurveTo(492, 635, 535, 868);
            ctx.lineTo(wfX + poolHalfW + 14, poolY);
            ctx.closePath();
            ctx.fill();

            const shadowFaceGrad = ctx.createLinearGradient(0, 440, 0, poolY);
            shadowFaceGrad.addColorStop(0, lerpRGBA([32, 56, 76], [7, 14, 28], nightMix, 0.82));
            shadowFaceGrad.addColorStop(1, lerpRGBA([20, 38, 52], [4, 9, 18],  nightMix, 0.45));
            ctx.fillStyle = shadowFaceGrad;
            ctx.beginPath();
            ctx.moveTo(118, 462);
            ctx.quadraticCurveTo(185, 505, wfX - topHalfW - 10, topY - 4);
            ctx.lineTo(wfX - poolHalfW - 18, poolY);
            ctx.lineTo(165, 868);
            ctx.closePath();
            ctx.fill();

            ctx.strokeStyle = lerpRGBA([248, 250, 252], [148, 163, 184], nightMix, lerp(0.7, 0.32, nightMix));
            ctx.lineWidth = 2.2;
            ctx.beginPath();
            ctx.moveTo(-40, 595);
            ctx.quadraticCurveTo(35, 528, 118, 462);
            ctx.quadraticCurveTo(178, 500, wfX - topHalfW - 8, topY - 4);
            ctx.moveTo(wfX + topHalfW + 8, topY - 6);
            ctx.quadraticCurveTo(385, 475, 458, 436);
            ctx.quadraticCurveTo(575, 488, 688, 514);
            ctx.quadraticCurveTo(790, 542, 915, 568);
            ctx.stroke();

            ctx.save();
            ctx.translate(wfX, topY);
            ctx.scale(topHalfW + 18, 11);
            const basinGrad = ctx.createRadialGradient(0, 0, 0.05, 0, 0, 1);
            basinGrad.addColorStop(0, lerpRGBA([224, 242, 254], [56, 189, 248], nightMix, 0.85));
            basinGrad.addColorStop(0.55, lerpRGBA([125, 211, 252], [30, 88, 138], nightMix, 0.42));
            basinGrad.addColorStop(1, 'rgba(56, 189, 248, 0)');
            ctx.fillStyle = basinGrad;
            ctx.beginPath();
            ctx.arc(0, 0, 1, 0, Math.PI * 2);
            ctx.fill();
            ctx.restore();

            const gorgeGrad = ctx.createLinearGradient(wfX, topY, wfX, poolY);
            gorgeGrad.addColorStop(0, lerpRGB([24, 42, 58], [5, 11, 22], nightMix));
            gorgeGrad.addColorStop(1, lerpRGB([16, 30, 42], [3, 8, 16],  nightMix));
            ctx.fillStyle = gorgeGrad;
            ctx.beginPath();
            ctx.moveTo(wfX - topHalfW - 14, topY);
            ctx.lineTo(wfX + topHalfW + 14, topY);
            ctx.quadraticCurveTo(wfX + midHalfW + 22, midLedgeY, wfX + poolHalfW + 20, poolY + 4);
            ctx.lineTo(wfX - poolHalfW - 20, poolY + 4);
            ctx.quadraticCurveTo(wfX - midHalfW - 22, midLedgeY, wfX - topHalfW - 14, topY);
            ctx.closePath();
            ctx.fill();

            const leftBankTopX = 205;
            const leftBankMidX = 216;
            const leftBankBotX = 226;
            const rightBankTopX = 345;
            const rightBankMidX = 412;
            const rightBankBotX = 496;
            const midChannelY = (poolY + riverJoinY) * 0.54;

            const tribGrad = ctx.createLinearGradient(wfX, poolY, (leftBankBotX + rightBankBotX) * 0.5, riverJoinY + 72);
            tribGrad.addColorStop(0.0, lerpRGBA([125, 211, 252], [30, 88, 138], nightMix, 0.96));
            tribGrad.addColorStop(0.32, lerpRGBA([56, 152, 198], [14, 52, 88], nightMix, 0.88));
            tribGrad.addColorStop(0.62, lerpRGBA([36, 108, 152], [10, 26, 50], nightMix, 0.45));
            tribGrad.addColorStop(0.85, lerpRGBA([36, 108, 152], [10, 26, 50], nightMix, 0.12));
            tribGrad.addColorStop(1.0, lerpRGBA([36, 108, 152], [10, 26, 50], nightMix, 0.0));
            ctx.fillStyle = tribGrad;
            ctx.beginPath();
            ctx.moveTo(leftBankTopX, poolY - 2);
            ctx.quadraticCurveTo(leftBankMidX, midChannelY, leftBankBotX - 55, riverJoinY + 68);
            ctx.lineTo(rightBankBotX + 95, riverJoinY + 68);
            ctx.quadraticCurveTo(rightBankMidX + 18, midChannelY, rightBankTopX, poolY - 2);
            ctx.closePath();
            ctx.fill();

            // Soft radial delta plume at confluence mouth so there is zero seam
            ctx.save();
            ctx.translate((leftBankBotX + rightBankBotX) * 0.5 + 16, riverJoinY + 18);
            ctx.scale(235, 68);
            const deltaGrad = ctx.createRadialGradient(0, 0, 0.04, 0, 0, 1);
            deltaGrad.addColorStop(0, lerpRGBA([125, 211, 252], [28, 78, 128], nightMix, lerp(0.54, 0.38, nightMix)));
            deltaGrad.addColorStop(0.45, lerpRGBA([56, 142, 188], [14, 42, 76], nightMix, lerp(0.28, 0.18, nightMix)));
            deltaGrad.addColorStop(0.78, lerpRGBA([36, 108, 152], [10, 26, 50], nightMix, lerp(0.10, 0.06, nightMix)));
            deltaGrad.addColorStop(1, 'rgba(36, 108, 152, 0)');
            ctx.fillStyle = deltaGrad;
            ctx.beginPath();
            ctx.arc(0, 0, 1, 0, Math.PI * 2);
            ctx.fill();
            ctx.restore();

            // 5 undulating subsurface water chutes curving smoothly into the downstream river
            for (let chute = 0; chute < 5; chute++) {
                const u = 0.12 + chute * 0.19;
                const sx = lerp(leftBankTopX, rightBankTopX, u);
                const mx = lerp(leftBankMidX, rightBankMidX, u) + Math.sin(animTime * 1.4 + chute * 1.7) * 5;
                const ex = lerp(leftBankBotX - 18, rightBankBotX + 55, u) + Math.cos(animTime * 1.1 + chute) * 8;

                const chuteGrad = ctx.createLinearGradient(sx, poolY, ex, riverJoinY + 52);
                chuteGrad.addColorStop(0, lerpRGBA([224, 242, 254], [125, 211, 252], nightMix, lerp(0.42, 0.24, nightMix)));
                chuteGrad.addColorStop(0.5, lerpRGBA([186, 230, 253], [56, 189, 248], nightMix, lerp(0.22, 0.12, nightMix)));
                chuteGrad.addColorStop(0.82, lerpRGBA([56, 152, 198], [14, 52, 88], nightMix, lerp(0.06, 0.03, nightMix)));
                chuteGrad.addColorStop(1, 'rgba(56, 189, 248, 0)');

                ctx.strokeStyle = chuteGrad;
                ctx.lineWidth = 10 + (chute % 2) * 4;
                ctx.lineCap = 'round';
                ctx.beginPath();
                ctx.moveTo(sx, poolY + 4);
                ctx.quadraticCurveTo(mx, midChannelY + 8, ex, riverJoinY + 48);
                ctx.stroke();
            }

            // 22 asynchronous current threads sweeping smoothly into the horizontal river flow
            ctx.save();
            ctx.lineCap = 'round';
            waterfallOutflowThreads.forEach((th, idx) => {
                const sx = lerp(leftBankTopX + 4, rightBankTopX - 4, th.u);
                const mx = lerp(leftBankMidX + 4, rightBankMidX - 4, th.u) + Math.sin(animTime * 1.6 + idx) * 3.5;
                const ex = lerp(leftBankBotX - 12, rightBankBotX + 64, th.u) + 32;

                const threadGrad = ctx.createLinearGradient(sx, poolY, ex, riverJoinY + 54);
                threadGrad.addColorStop(0.0, lerpRGBA([255, 255, 255], [186, 230, 253], nightMix, lerp(0.70, 0.42, nightMix)));
                threadGrad.addColorStop(0.48, lerpRGBA([224, 242, 254], [125, 211, 252], nightMix, lerp(0.36, 0.20, nightMix)));
                threadGrad.addColorStop(0.78, lerpRGBA([125, 211, 252], [56, 189, 248], nightMix, lerp(0.10, 0.05, nightMix)));
                threadGrad.addColorStop(1.0, 'rgba(125, 211, 252, 0)');

                ctx.strokeStyle = threadGrad;
                ctx.lineWidth = th.width;
                ctx.setLineDash([th.dashA, th.dashB]);
                ctx.lineDashOffset = -(animTime * th.speed + th.phase);
                ctx.beginPath();
                ctx.moveTo(sx, poolY + 4);
                ctx.quadraticCurveTo(mx, midChannelY + 14, ex, riverJoinY + 48);
                ctx.stroke();
            });
            ctx.restore();

            // 14 drifting whitewater foam wakes across 5 lanes (including far-left bank)
            for (let w = 0; w < 14; w++) {
                const laneIdx = w % 5;
                const laneU = 0.12 + laneIdx * 0.19;
                const cycle = (animTime * (0.28 + (w % 4) * 0.05) + w * 0.17) % 1;
                const oneMinus = 1 - cycle;
                const topX = lerp(leftBankTopX + 8, rightBankTopX - 8, laneU);
                const ctrlX = lerp(leftBankMidX + 8, rightBankMidX - 8, laneU);
                const botX = lerp(leftBankBotX - 4, rightBankBotX + 20, laneU);

                const wx = oneMinus * oneMinus * topX + 2 * oneMinus * cycle * ctrlX + cycle * cycle * botX;
                const wy = lerp(poolY + 8, riverJoinY + 26, cycle);
                const wrx = 14 + cycle * 28;
                const wry = 4 + cycle * 6.5;
                const wAlpha = Math.sin(cycle * Math.PI) * lerp(0.46, 0.28, nightMix);

                ctx.save();
                ctx.translate(wx, wy);
                ctx.scale(wrx, wry);
                const wakeGrad = ctx.createRadialGradient(0, 0, 0.1, 0, 0, 1);
                wakeGrad.addColorStop(0, lerpRGBA([255, 255, 255], [224, 242, 254], nightMix, wAlpha));
                wakeGrad.addColorStop(0.55, lerpRGBA([224, 242, 254], [125, 211, 252], nightMix, wAlpha * 0.55));
                wakeGrad.addColorStop(1, 'rgba(186, 230, 253, 0)');
                ctx.fillStyle = wakeGrad;
                ctx.beginPath();
                ctx.arc(0, 0, 1, 0, Math.PI * 2);
                ctx.fill();
                ctx.restore();
            }

            // Main Waterfall Body
            const wfGrad = ctx.createLinearGradient(wfX, topY, wfX, poolY);
            wfGrad.addColorStop(0.0, lerpRGBA([186, 230, 253], [96, 165, 250], nightMix, lerp(0.92, 0.52, nightMix)));
            wfGrad.addColorStop(0.45, lerpRGBA([224, 242, 254], [125, 211, 252], nightMix, lerp(0.95, 0.56, nightMix)));
            wfGrad.addColorStop(0.8, lerpRGBA([186, 230, 253], [56, 189, 248], nightMix, lerp(0.90, 0.48, nightMix)));
            wfGrad.addColorStop(1.0, lerpRGBA([255, 255, 255], [186, 230, 253], nightMix, lerp(0.98, 0.64, nightMix)));
            ctx.fillStyle = wfGrad;
            ctx.beginPath();
            ctx.moveTo(wfX - topHalfW, topY);
            ctx.quadraticCurveTo(wfX, topY + 6, wfX + topHalfW, topY);
            ctx.quadraticCurveTo(wfX + midHalfW - 4, (topY + midLedgeY) * 0.5, wfX + midHalfW, midLedgeY);
            ctx.quadraticCurveTo(wfX + poolHalfW - 6, (midLedgeY + poolY) * 0.5, wfX + poolHalfW, poolY);
            ctx.lineTo(wfX - poolHalfW, poolY);
            ctx.quadraticCurveTo(wfX - poolHalfW + 6, (midLedgeY + poolY) * 0.5, wfX - midHalfW, midLedgeY);
            ctx.quadraticCurveTo(wfX - midHalfW + 4, (topY + midLedgeY) * 0.5, wfX - topHalfW, topY);
            ctx.closePath();
            ctx.fill();

            // 5 wide undulating water chutes inside the vertical cascade
            for (let c = 0; c < 5; c++) {
                const cu = 0.14 + c * 0.18;
                const tx = lerp(wfX - topHalfW + 6, wfX + topHalfW - 6, cu);
                const mx = lerp(wfX - midHalfW + 8, wfX + midHalfW - 8, cu) + Math.sin(animTime * 1.8 + c * 1.5) * 4;
                const bx = lerp(wfX - poolHalfW + 10, wfX + poolHalfW - 10, cu) + Math.cos(animTime * 1.5 + c) * 5;

                const chuteGrad = ctx.createLinearGradient(tx, topY, bx, poolY);
                chuteGrad.addColorStop(0, lerpRGBA([255, 255, 255], [186, 230, 253], nightMix, lerp(0.55, 0.32, nightMix)));
                chuteGrad.addColorStop(0.5, lerpRGBA([224, 242, 254], [125, 211, 252], nightMix, lerp(0.40, 0.24, nightMix)));
                chuteGrad.addColorStop(1, lerpRGBA([255, 255, 255], [186, 230, 253], nightMix, lerp(0.65, 0.38, nightMix)));

                ctx.strokeStyle = chuteGrad;
                ctx.lineWidth = 8 + (c % 2) * 3;
                ctx.lineCap = 'round';
                ctx.beginPath();
                ctx.moveTo(tx, topY + 2 + (c % 3) * 3);
                ctx.quadraticCurveTo(mx, midLedgeY, bx, poolY - 3);
                ctx.stroke();
            }

            // Flanking rock walls
            ctx.fillStyle = lerpRGB([38, 66, 84], [10, 19, 34], nightMix);
            ctx.beginPath();
            ctx.moveTo(wfX - topHalfW - 28, topY - 4);
            ctx.lineTo(wfX - topHalfW + 4, topY + 2);
            ctx.lineTo(wfX - midHalfW + 4, midLedgeY - 12);
            ctx.lineTo(wfX - midHalfW + 2, midLedgeY + 35);
            ctx.lineTo(wfX - poolHalfW - 22, poolY + 4);
            ctx.closePath();
            ctx.fill();

            ctx.fillStyle = lerpRGB([46, 78, 98], [12, 24, 42], nightMix);
            ctx.beginPath();
            ctx.moveTo(wfX + topHalfW + 28, topY - 6);
            ctx.lineTo(wfX + topHalfW - 4, topY + 2);
            ctx.lineTo(wfX + midHalfW - 4, midLedgeY - 10);
            ctx.lineTo(wfX + midHalfW - 2, midLedgeY + 35);
            ctx.lineTo(wfX + poolHalfW + 22, poolY + 4);
            ctx.closePath();
            ctx.fill();

            // Non-uniform, organic falling water streams (staggered crest heights, speeds, and dash phases)
            ctx.save();
            ctx.lineCap = 'round';
            waterfallUpperStreams.forEach((st, idx) => {
                const topX = lerp(wfX - topHalfW + 4, wfX + topHalfW - 4, st.u);
                const midX = lerp(wfX - midHalfW + 6, wfX + midHalfW - 6, st.u) + Math.sin(animTime * st.swayFreq + idx) * st.swayAmp;
                const botX = lerp(wfX - poolHalfW + 8, wfX + poolHalfW - 8, st.u) + Math.cos(animTime * st.swayFreq + idx) * (st.swayAmp * 1.2);

                ctx.strokeStyle = lerpRGBA(
                    [255, 255, 255],
                    [224, 242, 254],
                    nightMix,
                    lerp(0.68 + (idx % 3) * 0.08, 0.38 + (idx % 3) * 0.06, nightMix)
                );
                ctx.lineWidth = st.width;
                ctx.setLineDash([st.dashA, st.dashB]);
                ctx.lineDashOffset = -(animTime * st.speed + st.phase);

                ctx.beginPath();
                ctx.moveTo(topX, topY + st.topDropOffset);
                ctx.quadraticCurveTo(midX, midLedgeY, botX, poolY - 4);
                ctx.stroke();
            });
            ctx.restore();

            // Uneven rocky crest whitewater lip plumes at the top of the waterfall
            for (let k = 0; k < 7; k++) {
                const ku = 0.1 + (k / 6) * 0.8;
                const kx = lerp(wfX - topHalfW + 6, wfX + topHalfW - 6, ku);
                const ky = topY + 2 + Math.sin(k * 2.1) * 3.5;
                const krx = 8 + Math.sin(animTime * 2.4 + k) * 2.2;
                const kry = 4.5 + Math.cos(animTime * 2.1 + k) * 1.4;

                ctx.save();
                ctx.translate(kx, ky);
                ctx.scale(krx, kry);
                const lipGrad = ctx.createRadialGradient(0, 0, 0.1, 0, 0, 1);
                lipGrad.addColorStop(0, lerpRGBA([255, 255, 255], [224, 242, 254], nightMix, lerp(0.85, 0.55, nightMix)));
                lipGrad.addColorStop(1, 'rgba(224, 242, 254, 0)');
                ctx.fillStyle = lipGrad;
                ctx.beginPath();
                ctx.arc(0, 0, 1, 0, Math.PI * 2);
                ctx.fill();
                ctx.restore();
            }

            // Mid-ledge mist
            for (let m = 0; m < 5; m++) {
                const mPhase = (animTime * 0.35 + m * 0.2) % 1;
                const mx = wfX + Math.sin(animTime * 0.9 + m * 2.1) * 24;
                const my = midLedgeY + 12 - mPhase * 34;
                const mrx = (midHalfW + 22) * (0.88 + mPhase * 0.35);
                const mry = 20 + mPhase * 15;
                const mAlpha = Math.sin(mPhase * Math.PI) * lerp(0.72, 0.48, nightMix);

                ctx.save();
                ctx.translate(mx, my);
                ctx.scale(mrx, mry);
                const midVaporGrad = ctx.createRadialGradient(0, 0, 0.1, 0, 0, 1);
                midVaporGrad.addColorStop(0, lerpRGBA([255, 255, 255], [226, 232, 240], nightMix, mAlpha));
                midVaporGrad.addColorStop(0.5, lerpRGBA([248, 250, 252], [186, 210, 235], nightMix, mAlpha * 0.82));
                midVaporGrad.addColorStop(1, 'rgba(186, 230, 253, 0)');
                ctx.fillStyle = midVaporGrad;
                ctx.beginPath();
                ctx.arc(0, 0, 1, 0, Math.PI * 2);
                ctx.fill();
                ctx.restore();
            }

            // Plunge pool apron & billowing mist
            ctx.save();
            ctx.translate(wfX, poolY + 2);
            ctx.scale(poolHalfW + 36, 26);
            const apronGrad = ctx.createRadialGradient(0, 0, 0.1, 0, 0, 1);
            apronGrad.addColorStop(0, lerpRGBA([255, 255, 255], [240, 249, 255], nightMix, lerp(0.98, 0.82, nightMix)));
            apronGrad.addColorStop(0.55, lerpRGBA([240, 249, 255], [186, 230, 253], nightMix, lerp(0.82, 0.58, nightMix)));
            apronGrad.addColorStop(1, 'rgba(186, 230, 253, 0)');
            ctx.fillStyle = apronGrad;
            ctx.beginPath();
            ctx.arc(0, 0, 1, 0, Math.PI * 2);
            ctx.fill();
            ctx.restore();

            for (let p = 0; p < 16; p++) {
                const cycle = (animTime * 0.24 + p * (1 / 16)) % 1;
                const sideDir = (p % 2 === 0 ? 1 : -1);
                const spreadFactor = Math.pow(cycle, 0.82);
                const spreadX = sideDir * (8 + spreadFactor * (poolHalfW + 98)) + Math.sin(animTime * 1.15 + p * 1.3) * 16;
                const liftY = -Math.sin(cycle * Math.PI) * 40 + Math.cos(animTime * 0.85 + p) * 6;
                const fogX = wfX + spreadX * 0.88;
                const fogY = poolY + 6 + liftY;
                const rx = 58 + cycle * 88;
                const ry = 25 + Math.sin(cycle * Math.PI) * 22;
                const alphaEnvelope = Math.sin(cycle * Math.PI) * lerp(0.88, 0.66, nightMix);

                ctx.save();
                ctx.translate(fogX, fogY);
                ctx.scale(rx, ry);
                const puffGrad = ctx.createRadialGradient(0, -0.08, 0.12, 0, 0, 1);
                puffGrad.addColorStop(0, lerpRGBA([255, 255, 255], [241, 245, 249], nightMix, alphaEnvelope));
                puffGrad.addColorStop(0.48, lerpRGBA([250, 252, 255], [203, 213, 225], nightMix, alphaEnvelope * 0.86));
                puffGrad.addColorStop(0.78, lerpRGBA([226, 232, 240], [148, 163, 184], nightMix, alphaEnvelope * 0.45));
                puffGrad.addColorStop(1, 'rgba(148, 163, 184, 0)');
                ctx.fillStyle = puffGrad;
                ctx.beginPath();
                ctx.arc(0, 0, 1, 0, Math.PI * 2);
                ctx.fill();
                ctx.restore();
            }
        }

        function drawTransitionalHouses(camX) {
            const corridorProgress = Math.min(1, smoothScrollProgress / 0.74);
            const currentWorldX = 520 + corridorProgress * (substationPort.x - 520);

            valleyHouses.forEach((h) => {
                if (h.x + h.w < camX - 150 || h.x > camX + 2650) return;

                const groundY = getValleyGroundY(h.x + h.w * 0.5) + h.yOff;
                const wallTopY = groundY - h.h;
                const roofPeakY = wallTopY - h.roofH;
                const centerX = h.x + h.w * 0.5;
                const houseEnergy = Math.min(1, Math.max(0, (currentWorldX - h.x) / 220));

                let dayWall = [138, 96, 66];
                let nightWall = [26, 24, 34];
                if (h.style === 'cottage') {
                    dayWall = [214, 202, 184];
                    nightWall = [32, 38, 54];
                } else if (h.style === 'suburban') {
                    dayWall = [203, 213, 225];
                    nightWall = [28, 37, 54];
                }
                ctx.fillStyle = lerpRGB(dayWall, nightWall, nightMix);
                ctx.fillRect(h.x, wallTopY, h.w, h.h);

                const dayRoof = h.style === 'cabin' ? [78, 54, 42] : [71, 85, 105];
                const nightRoof = h.style === 'cabin' ? [18, 16, 24] : [15, 23, 42];
                ctx.fillStyle = lerpRGB(dayRoof, nightRoof, nightMix);
                ctx.beginPath();
                ctx.moveTo(h.x - 5, wallTopY + 2);
                ctx.lineTo(centerX, roofPeakY);
                ctx.lineTo(h.x + h.w + 5, wallTopY + 2);
                ctx.closePath();
                ctx.fill();

                const padX = 6;
                const padY = 6;
                const cellW = (h.w - padX * 2) / h.windows;
                const cellH = (h.h - padY * 2) / h.floors;

                for (let f = 0; f < h.floors; f++) {
                    for (let w = 0; w < h.windows; w++) {
                        const wx = h.x + padX + w * cellW + 2;
                        const wy = wallTopY + padY + f * cellH + 1.5;
                        const ww = Math.max(4, cellW - 5);
                        const wh = Math.max(4, cellH - 4);
                        ctx.fillStyle = houseEnergy > 0.05
                            ? `rgba(253, 224, 71, ${0.92 * houseEnergy})`
                            : lerpRGBA([71, 85, 105], [30, 41, 59], nightMix, 0.65);
                        ctx.fillRect(wx, wy, ww, wh);
                    }
                }
            });
        }

        function drawValleyAndRiver(camX) {
            const spanStartX = Math.max(0, Math.floor((camX - 220) / 12) * 12);
            const spanEndX = Math.min(WORLD_WIDTH, spanStartX + 3000);

            ctx.beginPath();
            ctx.moveTo(spanStartX, getValleyGroundY(spanStartX) - 14);
            for (let x = spanStartX; x <= spanEndX; x += 14) {
                ctx.lineTo(x, getValleyGroundY(x) - 14);
            }
            ctx.lineTo(spanEndX, 1200);
            ctx.lineTo(spanStartX, 1200);
            ctx.closePath();

            const valleyGrad = ctx.createLinearGradient(0, 825, 0, 1065);
            valleyGrad.addColorStop(0,   lerpRGB([52, 108, 74], [12, 28, 40], nightMix));
            valleyGrad.addColorStop(0.55,lerpRGB([36, 82, 56],  [8, 20, 32],  nightMix));
            valleyGrad.addColorStop(1,   lerpRGB([22, 54, 38],  [5, 13, 23],  nightMix));
            ctx.fillStyle = valleyGrad;
            ctx.fill();

            ctx.beginPath();
            ctx.moveTo(spanStartX, getExactRiverTopY(spanStartX));
            for (let x = spanStartX; x <= spanEndX; x += 10) {
                ctx.lineTo(x, getExactRiverTopY(x));
            }
            ctx.lineTo(spanEndX, 1200);
            ctx.lineTo(spanStartX, 1200);
            ctx.closePath();

            const waterGrad = ctx.createLinearGradient(0, 1005, 0, 1200);
            waterGrad.addColorStop(0, lerpRGB([36, 108, 152], [10, 26, 50], nightMix));
            waterGrad.addColorStop(0.35, lerpRGB([42, 118, 162], [8, 22, 44], nightMix));
            waterGrad.addColorStop(1, lerpRGB([18, 68, 108],  [3, 10, 24],  nightMix));
            ctx.fillStyle = waterGrad;
            ctx.fill();

            drawAlpineHeadwatersWaterfall(camX);

            for (let i = 0; i < forestTrees.length; i++) {
                const tr = forestTrees[i];
                if (tr.rowIdx > 2) continue;
                if (tr.x < camX - 120 || tr.x > camX + 2650) continue;
                const sway = Math.sin(animTime * 1.55 + tr.x * 0.025 + tr.rowIdx) * (1.5 + tr.rowIdx * 0.25);
                drawRealisticSpruceTree(tr.x, tr.groundY, tr.treeH, tr.scale, tr.seed, sway, tr.rowIdx);
            }

            wildernessBoulders.forEach(b => {
                if (b.x < camX - 100 || b.x > camX + 2600) return;
                const by = getValleyGroundY(b.x) + b.yOff;
                ctx.fillStyle = lerpRGB([100, 116, 139], [30, 41, 59], nightMix);
                ctx.beginPath();
                ctx.ellipse(b.x, by, b.r, b.r * b.aspect, 0, Math.PI, 0);
                ctx.fill();
            });

            drawTransitionalHouses(camX);

            for (let i = 0; i < forestTrees.length; i++) {
                const tr = forestTrees[i];
                if (tr.rowIdx <= 2) continue;
                if (tr.x < camX - 120 || tr.x > camX + 2650) continue;
                const sway = Math.sin(animTime * 1.7 + tr.x * 0.025 + tr.rowIdx) * 2.2;
                drawRealisticSpruceTree(tr.x, tr.groundY, tr.treeH, tr.scale, tr.seed, sway, tr.rowIdx);
            }

            ctx.save();
            const flowStartX = Math.max(0, Math.floor((camX - 200) / 140) * 140);
            const flowEndX = Math.min(WORLD_WIDTH, flowStartX + 2900);

            for (let band = 0; band < 5; band++) {
                const bandDepth = 18 + band * 26;
                const flowSpeed = (22 + band * 5) * animTime;
                ctx.beginPath();
                for (let x = flowStartX; x <= flowEndX; x += 28) {
                    const shoreInfluence = Math.max(0, 1 - band * 0.22);
                    const baseShoreY = getExactRiverTopY(x);
                    const waveY = lerp(1030, baseShoreY, shoreInfluence) + bandDepth + Math.sin(x * 0.015 - animTime * 1.6 + band) * 1.8;
                    if (x === flowStartX) ctx.moveTo(x, waveY);
                    else ctx.lineTo(x, waveY);
                }
                ctx.setLineDash([58 + band * 14, 110 + band * 18]);
                ctx.lineDashOffset = -flowSpeed;
                ctx.strokeStyle = lerpRGBA([186, 230, 253], [56, 189, 248], nightMix, 0.16);
                ctx.lineWidth = 1.4;
                ctx.stroke();
            }
            ctx.restore();
        }

        function getTowerWirePoint(index, isLowerCircuit, sideOffset = 0) {
            if (index >= towers.length) {
                return {
                    x: substationPort.x + sideOffset * 0.35,
                    y: isLowerCircuit ? substationPort.y + 32 : substationPort.y
                };
            }
            const t = towers[index];
            const armRatio = isLowerCircuit ? 0.63 : 0.83;
            const armY = t.baseY - t.h * armRatio;
            const insulatorSway = Math.sin(animTime * 1.8 + index * 0.9) * 1.6;
            return {
                x: t.x + sideOffset + insulatorSway,
                y: armY + 18
            };
        }

        function drawLatticeTowers(camX) {
            const totalSpans = towers.length;
            const corridorProgress = Math.min(1, smoothScrollProgress / 0.74);
            const activeTowerFloat = corridorProgress * totalSpans;

            towers.forEach((t, idx) => {
                if (t.x < camX - 280 || t.x > camX + 2750) return;

                const topY = t.baseY - t.h;
                const waistY = t.baseY - t.h * 0.56;
                const cageTopY = t.baseY - t.h * 0.88;

                const baseHalfW = 28;
                const waistHalfW = 11.5;
                const cageHalfW = 14;

                const towerEnergy = Math.max(0, Math.min(1, 1 - Math.abs(activeTowerFloat - idx) * 0.85));
                const isTowerPassed = activeTowerFloat >= idx;
                const isFaultTower = isFaultActive && !isRerouted && (
                    (activeScenarioId === 1 && (idx === 2 || idx === 3)) ||
                    (activeScenarioId === 2 && (idx === 4 || idx === 5))
                );

                const getHalfWidthAt = (f) => {
                    if (f < 0.56) return lerp(baseHalfW, waistHalfW, f / 0.56);
                    if (f < 0.88) return lerp(waistHalfW, cageHalfW, (f - 0.56) / 0.32);
                    return lerp(cageHalfW, 2.0, (f - 0.88) / 0.12);
                };

                const baseSteelRGB = lerpRGB([51, 65, 85], [115, 134, 158], nightMix);
                const energizedSteelColor = isFaultTower
                    ? '#f87171'
                    : (towerEnergy > 0.15 ? lerpRGB([2, 132, 199], [125, 211, 252], towerEnergy) : baseSteelRGB);

                ctx.strokeStyle = energizedSteelColor;
                ctx.lineWidth = 2.4;
                ctx.beginPath();
                ctx.moveTo(t.x - baseHalfW, t.baseY);
                ctx.lineTo(t.x - waistHalfW, waistY);
                ctx.lineTo(t.x - cageHalfW, cageTopY);
                ctx.lineTo(t.x, topY);
                ctx.moveTo(t.x + baseHalfW, t.baseY);
                ctx.lineTo(t.x + waistHalfW, waistY);
                ctx.lineTo(t.x + cageHalfW, cageTopY);
                ctx.lineTo(t.x, topY);
                ctx.stroke();

                ctx.lineWidth = 1.15;
                const bays = 9;
                for (let i = 0; i < bays; i++) {
                    const f1 = i / bays;
                    const f2 = (i + 1) / bays;
                    const y1 = lerp(t.baseY, topY, f1);
                    const y2 = lerp(t.baseY, topY, f2);
                    const w1 = getHalfWidthAt(f1);
                    const w2 = getHalfWidthAt(f2);

                    ctx.beginPath();
                    ctx.moveTo(t.x - w1, y1);
                    ctx.lineTo(t.x + w2, y2);
                    ctx.moveTo(t.x + w1, y1);
                    ctx.lineTo(t.x - w2, y2);
                    ctx.moveTo(t.x - w2, y2);
                    ctx.lineTo(t.x + w2, y2);
                    ctx.stroke();
                }

                [
                    { ratio: 0.83, span: 42, isLower: false },
                    { ratio: 0.63, span: 52, isLower: true }
                ].forEach(arm => {
                    const armY = t.baseY - t.h * arm.ratio;
                    const cageW = getHalfWidthAt(arm.ratio);

                    ctx.strokeStyle = energizedSteelColor;
                    ctx.lineWidth = 2.0;
                    ctx.beginPath();
                    ctx.moveTo(t.x - arm.span, armY);
                    ctx.lineTo(t.x + arm.span, armY);
                    ctx.moveTo(t.x - arm.span, armY);
                    ctx.lineTo(t.x - cageW, armY - 13);
                    ctx.moveTo(t.x + arm.span, armY);
                    ctx.lineTo(t.x + cageW, armY - 13);
                    ctx.stroke();

                    const insulatorSway = Math.sin(animTime * 1.8 + idx * 0.9) * 1.6;
                    [-arm.span, 0, arm.span].forEach((offsetX, phaseIdx) => {
                        const anchorX = t.x + offsetX * (phaseIdx === 1 ? 0 : 0.92);
                        const tipX = anchorX + insulatorSway;
                        const tipY = armY + 18;

                        ctx.fillStyle = isFaultTower ? '#fca5a5' : (isTowerPassed ? '#7dd3fc' : '#94a3b8');
                        for (let d = 1; d <= 4; d++) {
                            const df = d / 4.5;
                            ctx.beginPath();
                            ctx.ellipse(lerp(anchorX, tipX, df), lerp(armY, tipY, df), 3.4, 1.3, 0, 0, Math.PI * 2);
                            ctx.fill();
                        }

                        if (phaseIdx === 1 && (isTowerPassed || towerEnergy > 0.2)) {
                            const coronaIntensity = Math.max(towerEnergy, isTowerPassed ? 0.55 : 0);
                            const coronaGrad = ctx.createRadialGradient(tipX, tipY, 1, tipX, tipY, 15);
                            const cColor = isFaultTower
                                ? '239, 68, 68'
                                : (arm.isLower && isRerouted ? '16, 185, 129' : '56, 189, 248');
                            coronaGrad.addColorStop(0, `rgba(255, 255, 255, ${0.9 * coronaIntensity})`);
                            coronaGrad.addColorStop(0.4, `rgba(${cColor}, ${0.65 * coronaIntensity})`);
                            coronaGrad.addColorStop(1, `rgba(${cColor}, 0)`);
                            ctx.fillStyle = coronaGrad;
                            ctx.beginPath();
                            ctx.arc(tipX, tipY, 15, 0, Math.PI * 2);
                            ctx.fill();
                        }
                    });
                });

                ctx.font = '600 10px "JetBrains Mono", monospace';
                ctx.fillStyle = 'rgba(148, 163, 184, 0.85)';
                ctx.fillText(t.id, t.x - 7, t.baseY + 18);

                ctx.fillStyle = '#ef4444';
                ctx.beginPath();
                ctx.arc(t.x, topY - 3, 3.2, 0, Math.PI * 2);
                ctx.fill();
            });
        }

        function sampleCatenarySpan(p1, p2, t, sag) {
            const mt = 1 - t;
            const cx = (p1.x + p2.x) * 0.5;
            const cy = (p1.y + p2.y) * 0.5 + sag;
            return {
                x: mt * mt * p1.x + 2 * mt * t * cx + t * t * p2.x,
                y: mt * mt * p1.y + 2 * mt * t * cy + t * t * p2.y
            };
        }

        function drawTransmissionLinesAndFlow() {
            const totalSpans = towers.length;

            [false, true].forEach(isLower => {
                for (let s = 0; s < totalSpans; s++) {
                    const p1 = getTowerWirePoint(s, isLower);
                    const p2 = getTowerWirePoint(s + 1, isLower);
                    const sag = 38 + s * 2.5;

                    const isFaultSpan = !isLower && isFaultActive && (
                        (activeScenarioId === 1 && s === 2) ||
                        (activeScenarioId === 2 && s === 4)
                    );
                    const isBackupReroute = isLower && isRerouted;

                    ctx.beginPath();
                    ctx.moveTo(p1.x, p1.y);
                    ctx.quadraticCurveTo((p1.x + p2.x) / 2, (p1.y + p2.y) / 2 + sag, p2.x, p2.y);

                    if (isFaultSpan) {
                        ctx.strokeStyle = `rgba(239, 68, 68, ${0.55 + Math.sin(animTime * 16) * 0.4})`;
                        ctx.lineWidth = 3.2;
                    } else if (isBackupReroute) {
                        ctx.strokeStyle = 'rgba(16, 185, 129, 0.85)';
                        ctx.lineWidth = 3.0;
                    } else {
                        ctx.strokeStyle = lerpRGBA([30, 41, 59], [56, 189, 248], nightMix, lerp(0.58, 0.45, nightMix));
                        ctx.lineWidth = 2.0;
                    }
                    ctx.stroke();

                    ctx.save();
                    ctx.beginPath();
                    ctx.moveTo(p1.x, p1.y);
                    ctx.quadraticCurveTo((p1.x + p2.x) / 2, (p1.y + p2.y) / 2 + sag, p2.x, p2.y);
                    ctx.setLineDash([24, 64]);
                    ctx.lineDashOffset = -animTime * (isBackupReroute ? 135 : 75);
                    ctx.strokeStyle = isFaultSpan
                        ? '#ef4444'
                        : isBackupReroute
                            ? '#10b981'
                            : lerpRGB([2, 132, 199], [0, 242, 254], nightMix);
                    ctx.lineWidth = isBackupReroute ? 3.2 : 2.4;
                    ctx.stroke();
                    ctx.restore();
                }
            });

            const corridorProgress = Math.min(1, smoothScrollProgress / 0.74);
            const globalFloat = corridorProgress * totalSpans;
            const activeSpanIndex = Math.min(totalSpans - 1, Math.floor(globalFloat));
            const localT = corridorProgress >= 1 ? 1 : (globalFloat - activeSpanIndex);

            const pStart = getTowerWirePoint(activeSpanIndex, isRerouted);
            const pEnd = getTowerWirePoint(activeSpanIndex + 1, isRerouted);
            const headPt = sampleCatenarySpan(pStart, pEnd, localT, 38 + activeSpanIndex * 2.5);

            const orbGrad = ctx.createRadialGradient(headPt.x, headPt.y, 1, headPt.x, headPt.y, 11);
            orbGrad.addColorStop(0,   '#ffffff');
            orbGrad.addColorStop(0.32,'#fef08a');
            orbGrad.addColorStop(0.68,'#00f2fe');
            orbGrad.addColorStop(1,   'rgba(0, 242, 254, 0)');
            ctx.fillStyle = orbGrad;
            ctx.beginPath();
            ctx.arc(headPt.x, headPt.y, 11, 0, Math.PI * 2);
            ctx.fill();

            if (lightningFlashAlpha > 0.01) {
                const spanIdx = activeScenarioId === 2 ? 4 : 2;
                const f1 = getTowerWirePoint(spanIdx, false);
                const f2 = getTowerWirePoint(spanIdx + 1, false);
                const strikePt = sampleCatenarySpan(f1, f2, 0.5, 43);

                ctx.save();
                ctx.strokeStyle = `rgba(254, 240, 138, ${lightningFlashAlpha})`;
                ctx.lineWidth = 4.5;
                ctx.shadowColor = '#f59e0b';
                ctx.shadowBlur = 24;
                ctx.beginPath();
                ctx.moveTo(strikePt.x - 35, 120);
                ctx.lineTo(strikePt.x + 15, 310);
                ctx.lineTo(strikePt.x - 22, 460);
                ctx.lineTo(strikePt.x + 18, 620);
                ctx.lineTo(strikePt.x, strikePt.y);
                ctx.stroke();
                ctx.restore();
            }
        }

        function drawMetropolitanSmartCity(camX) {
            if (camX + 2600 < 5350) return;

            const cityWaveProgress = Math.min(1, Math.max(0, (smoothScrollProgress - 0.74) / 0.24));
            const surgeCurrentX = 5560 + cityWaveProgress * 1680;

            bgCityBuildings.forEach((bg) => {
                const topY = 990 - bg.h;
                ctx.fillStyle = lerpRGB([51, 65, 85], [8, 13, 26], nightMix);
                ctx.fillRect(bg.x, topY, bg.w, bg.h);
            });

            ctx.fillStyle = lerpRGB([51, 65, 85], [15, 23, 42], nightMix);
            ctx.fillRect(5420, 990, 1820, 42);

            cityBuildings.forEach((b, bIdx) => {
                const topY = 990 - b.h;
                const bEnergy = Math.min(1, Math.max(0, (surgeCurrentX - b.x) / (b.w + 65)));

                ctx.fillStyle = lerpRGB([51, 65, 85], [12, 19, 36], nightMix);
                ctx.fillRect(b.x, topY, b.w, b.h);

                ctx.strokeStyle = bEnergy > 0.1
                    ? `rgba(56, 189, 248, ${0.3 + bEnergy * 0.55})`
                    : `rgba(148, 163, 184, 0.25)`;
                ctx.lineWidth = 1.4;
                ctx.strokeRect(b.x, topY, b.w, b.h);

                const padX = 10;
                const padY = b.isHospital ? 90 : 20;
                const cellW = (b.w - padX * 2) / b.cols;
                const cellH = (b.h - padY - 16) / b.floors;

                for (let r = 0; r < b.floors; r++) {
                    const floorFromBottom = (b.floors - 1 - r) / b.floors;
                    const floorGlow = bEnergy > floorFromBottom ? Math.min(1, (bEnergy - floorFromBottom) * 3.5) : 0;

                    for (let c = 0; c < b.cols; c++) {
                        const seed = (bIdx * 31 + r * 17 + c * 13) % 10;
                        if (seed < 2) continue;
                        const wx = b.x + padX + c * cellW + 1.5;
                        const wy = topY + padY + r * cellH + 1.5;
                        if (floorGlow > 0.01) {
                            ctx.fillStyle = b.isHospital
                                ? `rgba(110, 231, 183, ${0.95 * floorGlow})`
                                : `rgba(254, 240, 138, ${0.92 * floorGlow})`;
                            ctx.fillRect(wx, wy, Math.max(2, cellW - 4), Math.max(2, cellH - 3.5));
                        }
                    }
                }

                if (b.isHospital) {
                    const cx = b.x + b.w * 0.5;
                    const cy = topY + 46;
                    ctx.fillStyle = '#10b981';
                    ctx.fillRect(cx - 8, cy - 24, 16, 48);
                    ctx.fillRect(cx - 24, cy - 8, 48, 16);
                }
            });
        }

        function updateUIReadouts() {
            const pct = Math.round(smoothScrollProgress * 100);
            const pctEl = document.getElementById('scroll-pct-readout');
            if (pctEl) pctEl.textContent = `${pct}%`;

            const thresholds = [0, 0.28, 0.58, 0.76];
            for (let i = 0; i < 4; i++) {
                const btn = document.getElementById(`station-${i}`);
                const isNext = i < 3 ? smoothScrollProgress < thresholds[i + 1] : true;
                if (smoothScrollProgress >= thresholds[i] && isNext) {
                    if (btn) btn.classList.add('active');
                } else if (btn) {
                    btn.classList.remove('active');
                }
            }

            const heroBrand = document.getElementById('hero-brand');
            const heroLeft = document.getElementById('hero-left-cluster');
            if (heroBrand && heroLeft) {
                const targetHeroAlpha = 1 - smoothstep(0.008, 0.085, smoothScrollProgress);
                smoothHeroAlpha += (targetHeroAlpha - smoothHeroAlpha) * 0.14;
                heroBrand.style.opacity = smoothHeroAlpha.toFixed(3);
                heroLeft.style.pointerEvents = smoothHeroAlpha > 0.35 ? 'auto' : 'none';
            }

            scrollStageWindows.forEach((stage) => {
                const el = document.getElementById(stage.id);
                if (!el) return;
                const fadeIn = smoothstep(stage.start, stage.peakStart, smoothScrollProgress);
                const fadeOut = 1 - smoothstep(stage.peakEnd, stage.end, smoothScrollProgress);
                const targetOpacity = Math.max(0, Math.min(1, fadeIn * fadeOut));
                stage.curOpacity += (targetOpacity - stage.curOpacity) * 0.14;
                el.style.opacity = stage.curOpacity.toFixed(3);
            });
        }

        function renderScene(timestamp) {
            // The corridor canvas is only visible on the landing view; keep the loop alive but skip the drawing work elsewhere.
            if (activeAppView !== 'landing') {
                requestAnimationFrame(renderScene);
                return;
            }
            animTime = (timestamp || 0) * 0.001;
            smoothScrollProgress += (targetScrollProgress - smoothScrollProgress) * 0.09;
            nightMix += ((isNightMode ? 1.0 : 0.0) - nightMix) * 0.07;

            if (lightningFlashAlpha > 0) {
                lightningFlashAlpha = Math.max(0, lightningFlashAlpha - 0.035);
            }

            const scale = canvas.height > 0 ? canvas.height / WORLD_HEIGHT : 1;
            const viewWorldW = scale > 0 ? canvas.width / scale : 1920;
            const maxCamX = Math.max(0, WORLD_WIDTH - viewWorldW);
            const camX = smoothScrollProgress * maxCamX;

            ctx.save();
            ctx.scale(scale, scale);
            ctx.translate(-camX, 0);

            drawSkyAndAtmosphere(camX, viewWorldW);
            drawMountainRange(camX, 1.2, 470, 210, 0.35, [92, 126, 162], [11, 22, 44], true);
            drawValleyMist(camX, 595, 55);
            drawMountainRange(camX, 3.8, 610, 175, 0.58, [64, 98, 128],  [14, 27, 50], true);
            drawValleyMist(camX, 745, 45);
            drawMountainRange(camX, 6.4, 735, 130, 0.80, [45, 78, 96],   [12, 22, 38], false);

            drawValleyAndRiver(camX);
            drawLatticeTowers(camX);
            drawTransmissionLinesAndFlow();
            drawMetropolitanSmartCity(camX);

            ctx.restore();

            updateUIReadouts();
            requestAnimationFrame(renderScene);
        }

        function setScrollProgress(newProgress) {
            targetScrollProgress = Math.max(0, Math.min(1, newProgress));
        }

        window.scrollToStation = function (progress) {
            setScrollProgress(progress);
        };

        window.toggleDayNight = function () {
            isNightMode = !isNightMode;
            localStorage.setItem('gridpulse_theme', isNightMode ? 'dark' : 'light');
            const htmlEl = document.documentElement;
            const knobs = document.querySelectorAll('.theme-knob');
            if (isNightMode) {
                htmlEl.classList.add('dark');
                knobs.forEach(k => { k.style.transform = 'translateX(24px)'; });
            } else {
                htmlEl.classList.remove('dark');
                knobs.forEach(k => { k.style.transform = 'translateX(0px)'; });
            }
        };

        let activeAppView = 'landing';
        let consoleScenario = 0;
        let consoleMitigated = false;

        function setAppView(viewName) {
            if (viewName === activeAppView) return;
            activeAppView = viewName;
            const views = {
                landing: document.getElementById('view-landing'),
                gateway: document.getElementById('view-gateway'),
                console: document.getElementById('view-console')
            };
            Object.keys(views).forEach(k => {
                const el = views[k];
                if (!el) return;
                if (k === viewName) {
                    el.classList.remove('view-hidden');
                    el.classList.add('view-active');
                } else {
                    el.classList.remove('view-active');
                    el.classList.add('view-hidden');
                }
            });
            if (viewName === 'console') {
                setTimeout(resizeScadaTopologyCanvas, 60);
            }
        }

        window.transitionToGateway = function () {
            setAppView('gateway');
        };

        window.returnToCorridor = function () {
            setAppView('landing');
        };


        window.toggleScadaMenu = function () {
            const drawer = document.getElementById('scada-menu-drawer');
            if (!drawer) return;
            const open = drawer.classList.toggle('-translate-x-full') === false;
            // A closed drawer is only moved off-screen, so take it out of the tab order too.
            drawer.inert = !open;
            document.getElementById('scada-menu-button')?.setAttribute('aria-expanded', String(open));
            if (open) drawer.querySelector('button')?.focus();
        };

        window.switchScadaPage = function (pageId) {
            const pages = ['topology', 'cypher', 'mcp', 'audit', 'askgrid', 'networks', 'learning'];
            const labels = {
                topology: '01 / LIVE TOPOLOGY',
                cypher: '02 / CYPHER STUDIO',
                mcp: '03 / SCADA MCP AGENT',
                audit: '04 / NERC CIP AUDIT',
                askgrid: '05 / ASK THE GRID',
                networks: '06 / NETWORK LAB',
                learning: '07 / GRID MEMORY'
            };
            pages.forEach(p => {
                const el = document.getElementById(`scada-page-${p}`);
                if (el) el.classList.toggle('hidden', p !== pageId);
            });
            const badge = document.getElementById('active-page-badge');
            if (badge && labels[pageId]) badge.textContent = labels[pageId];
            document.querySelectorAll('[data-scada-nav]').forEach(button => {
                const active = button.dataset.scadaNav === pageId;
                button.classList.toggle('scada-nav-active', active);
                button.setAttribute('aria-current', active ? 'page' : 'false');
            });
            const drawer = document.getElementById('scada-menu-drawer');
            if (drawer) drawer.classList.add('-translate-x-full');
            if (pageId === 'topology') setTimeout(resizeScadaTopologyCanvas, 50);
        };


        const scadaCanvas = document.getElementById('scada-topology-canvas');
        const sCtx = scadaCanvas ? scadaCanvas.getContext('2d') : null;

        function resizeScadaTopologyCanvas() {
            if (!scadaCanvas || !scadaCanvas.parentElement) return;
            const rect = scadaCanvas.parentElement.getBoundingClientRect();
            const dpr = Math.min(window.devicePixelRatio || 1, 2);
            scadaCanvas.width = Math.max(800, rect.width) * dpr;
            scadaCanvas.height = Math.max(200, rect.height * dpr);
        }
        window.addEventListener('resize', resizeScadaTopologyCanvas);

        function renderScadaTopology() {
            if (activeAppView === 'console' && window.gridpulseDrawTopology) window.gridpulseDrawTopology(scadaCanvas, sCtx);
            requestAnimationFrame(renderScadaTopology);
        }


        window.toggleTelemetryDrawer = function () {
            isDrawerOpen = !isDrawerOpen;
            const drawer = document.getElementById('telemetry-drawer');
            if (!drawer) return;
            if (isDrawerOpen) {
                drawer.classList.remove('translate-y-[120%]', 'opacity-0', 'pointer-events-none');
            } else {
                drawer.classList.add('translate-y-[120%]', 'opacity-0', 'pointer-events-none');
            }
        };

        window.selectCypherTab = function (idx) {
            activeCypherIndex = idx;
            const box = document.getElementById('cypher-code-box');
            if (box) box.textContent = cypherQueries[idx];
            for (let i = 0; i < 3; i++) {
                const tab = document.getElementById(`cypher-tab-${i}`);
                if (!tab) continue;
                tab.className = i === idx
                    ? 'px-2 py-1 rounded text-[10px] font-mono bg-sky-500/20 text-sky-300 border border-sky-500/40'
                    : 'px-2 py-1 rounded text-[10px] font-mono text-slate-400 hover:text-slate-200 border border-transparent';
            }
        };

        window.copyCypherCode = function () {
            const tempInput = document.createElement('textarea');
            tempInput.value = cypherQueries[activeCypherIndex];
            document.body.appendChild(tempInput);
            tempInput.select();
            document.execCommand('copy');
            document.body.removeChild(tempInput);
            const copyBtn = document.getElementById('copy-cypher-btn');
            if (copyBtn) {
                copyBtn.textContent = 'Copied!';
                setTimeout(() => { copyBtn.textContent = 'Copy'; }, 1400);
            }
        };


        window.addEventListener('wheel', (e) => {
            if (activeAppView !== 'landing') return;
            if (e.target && e.target.closest && e.target.closest('#cypher-code-box')) return;
            e.preventDefault();
            let rawDelta = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
            if (e.deltaMode === 1) rawDelta *= 32;
            else if (e.deltaMode === 2) rawDelta *= window.innerHeight;

            const step = Math.max(-0.08, Math.min(0.08, rawDelta * 0.00055));
            if (targetScrollProgress >= 0.995 && rawDelta > 45) {
                transitionToGateway();
                return;
            }
            setScrollProgress(targetScrollProgress + step);
        }, { passive: false });

        let isDraggingScene = false;
        let dragStartX = 0;
        let dragStartProgress = 0;

        window.addEventListener('pointerdown', (e) => {
            if (activeAppView !== 'landing') return;
            if (e.target && e.target.closest && e.target.closest('header, main, aside, section, button, a, label, input')) return;
            isDraggingScene = true;
            dragStartX = e.clientX;
            dragStartProgress = targetScrollProgress;
        });

        window.addEventListener('pointermove', (e) => {
            if (!isDraggingScene || activeAppView !== 'landing') return;
            const dx = dragStartX - e.clientX;
            setScrollProgress(dragStartProgress + (dx / Math.max(320, window.innerWidth)) * 0.55);
        });

        const endSceneDrag = () => {
            isDraggingScene = false;
        };
        window.addEventListener('pointerup', endSceneDrag);
        window.addEventListener('pointercancel', endSceneDrag);

        window.addEventListener('keydown', (e) => {
            if (activeAppView !== 'landing') return;
            if (e.key === 'ArrowRight' || e.key === 'ArrowDown') {
                e.preventDefault();
                setScrollProgress(targetScrollProgress + 0.06);
            } else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
                e.preventDefault();
                setScrollProgress(targetScrollProgress - 0.06);
            }
        });

        window.onload = function () {
            resizeCanvas();
            resizeScadaTopologyCanvas();
            requestAnimationFrame(renderScene);
            requestAnimationFrame(renderScadaTopology);

            const savedTheme = localStorage.getItem('gridpulse_theme');
            if (savedTheme === 'light') window.toggleDayNight();
        };
