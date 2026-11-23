import { useCallback, useRef, useState, type CSSProperties, type PointerEvent, type WheelEvent } from "react";
import { useTranslation } from "react-i18next";
import { ImageOff, ZoomIn, ZoomOut } from "lucide-react";
import { cn } from "cn";
import { Button } from "@/components/ui/button";

/** 缩放倍率上下限。下限 1 即「适应容器」，不提供缩到比适应更小。 */
const MIN_ZOOM = 1;
const MAX_ZOOM = 6;
/** 每次点按钮或滚轮的倍率步长；捏合手势直接用两指间距比例。 */
const ZOOM_STEP = 1.5;
/** 双指手势判定：两根手指同向移动算横滑，反向移动算捏合。倍数 2 用于拉开两者差距。 */
const GESTURE_DOMINANCE = 2;
/** 两根手指各自的位移都达到这个像素才下判断，避免手指轻颤就定成某一种手势。 */
const GESTURE_DECIDE_AT = 8;
/** 双指横向移动超过这个像素才算切图，避免捏合时的轻微漂移误触。 */
const SWIPE_TRIGGER = 64;

type Point = { x: number; y: number };
type SwipeDirection = "prev" | "next";

type Gesture = {
  startDistance: number;
  startZoom: number;
  startPan: Point;
  startPoint: Point;
  /** 两根手指各自的起始位置，用来区分「同向平移」与「反向捏合」。 */
  startPoints: Point[];
  /** 本次手势已被判定成哪种：捏合与横滑二选一，避免边缩放边切图。 */
  mode: "none" | "pinch" | "swipe";
  /** 横滑模式下两指中点的横向位移，松手时据此决定是否切图。 */
  swipeDx: number;
};

const midX = (points: Point[]) => (points[0].x + points[1].x) / 2;

/**
 * 图片查看器的缩放与平移层：双指捏合、滚轮缩放，放大后可拖动。
 * 倍率回到 1 时自动归位，避免图被拖出可视区后找不回来。
 *
 * 换图时由调用处传 `key={src}` 让本组件重挂载，倍率与位移随之归位；
 * 不在这里用 effect 重置，渲染期改 ref 与 effect 里 setState 都会被 lint 拦下。
 */
export function ZoomableImage({
  src,
  alt,
  className,
  onNaturalSize,
  onSwipe,
  compact = false,
}: {
  src: string;
  alt: string;
  className?: string;
  /** 加载完成时报出原图像素尺寸。 */
  onNaturalSize?: (size: { width: number; height: number }) => void;
  /** 双指横向滑动：向左滑看下一张，向右滑看上一张。适应容器时才启用，放大后横滑用来平移。 */
  onSwipe?: (direction: SwipeDirection) => void;
  /** 全屏看图：省掉底部的按钮行，缩放只靠手势与滚轮。 */
  compact?: boolean;
}) {
  const { t } = useTranslation("assets");
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState<Point>({ x: 0, y: 0 });
  // 加载失败就显示占位，不让上一张的残留冒充当前这张
  const [failed, setFailed] = useState(false);
  // 拖动与捏合都基于手势开始时的状态累加，避免逐帧读取会抖动的当前值。
  const gesture = useRef<Gesture | null>(null);
  const activePointers = useRef(new Map<number, Point>());

  // 换图时归位，否则上一张的倍率会套到新图上。按 src 记住已归位的那张，
  // 用渲染期比较代替 effect 里的 setState。
  const [settledSrc, setSettledSrc] = useState(src);
  if (settledSrc !== src) {
    setSettledSrc(src);
    setZoom(1);
    setPan({ x: 0, y: 0 });
    setFailed(false);
  }

  const clampZoom = (value: number) => Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, value));

  const applyZoom = useCallback((nextZoom: number) => {
    const clamped = clampZoom(nextZoom);
    setZoom(clamped);
    // 回到适应容器时清掉位移，图重新居中
    if (clamped === MIN_ZOOM) setPan({ x: 0, y: 0 });
  }, []);

  const distanceBetween = (points: Point[]) => Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y);

  const handlePointerDown = (event: PointerEvent<HTMLDivElement>) => {
    activePointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    // 捕获指针，手指滑出舞台也能继续拖动；环境不支持时（旧浏览器、测试环境）退化为不捕获
    event.currentTarget.setPointerCapture?.(event.pointerId);
    const points = [...activePointers.current.values()];
    if (points.length === 2) {
      gesture.current = {
        startDistance: distanceBetween(points),
        startZoom: zoom,
        startPan: pan,
        startPoint: points[0],
        startPoints: points.map((point) => ({ ...point })),
        mode: "none",
        swipeDx: 0,
      };
    } else if (points.length === 1) {
      gesture.current = {
        startDistance: 0,
        startZoom: zoom,
        startPan: pan,
        startPoint: points[0],
        startPoints: [{ ...points[0] }],
        mode: "none",
        swipeDx: 0,
      };
    }
  };

  const handlePointerMove = (event: PointerEvent<HTMLDivElement>) => {
    if (!activePointers.current.has(event.pointerId) || !gesture.current) return;
    activePointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    const points = [...activePointers.current.values()];
    const start = gesture.current;
    if (points.length >= 2 && start.startDistance > 0) {
      const ratio = distanceBetween(points) / start.startDistance;
      // 手势方向还没定：两指的 move 事件逐根触发，只看一次移动会把「先动的那根」误当成捏合。
      // 等两根手指都各自移动够远，再比较同向位移（横滑）与反向位移（捏合）谁更大。
      if (start.mode === "none") {
        const d1 = points[0].x - start.startPoints[0].x;
        const d2 = points[1].x - start.startPoints[1].x;
        if (Math.min(Math.abs(d1), Math.abs(d2)) < GESTURE_DECIDE_AT) return;
        const parallel = Math.abs(d1 + d2) / 2;
        const opposite = Math.abs(d1 - d2) / 2;
        start.mode = parallel > opposite * GESTURE_DOMINANCE ? "swipe" : "pinch";
      }
      if (start.mode === "pinch") {
        applyZoom(start.startZoom * ratio);
        return;
      }
      // 横滑切图只在适应容器时生效；放大后双指横移仍用于平移
      if (zoom > MIN_ZOOM) return;
      start.swipeDx = midX(points) - midX(start.startPoints);
      return;
    }
    // 单指拖动：只在放大后平移，适应容器时保持居中
    if (zoom > MIN_ZOOM) {
      setPan({
        x: start.startPan.x + (points[0].x - start.startPoint.x),
        y: start.startPan.y + (points[0].y - start.startPoint.y),
      });
    }
  };

  const endPointer = (event: PointerEvent<HTMLDivElement>) => {
    const start = gesture.current;
    activePointers.current.delete(event.pointerId);
    const points = [...activePointers.current.values()];
    // 双指横滑：任意一根手指抬起时就判定，不等两根都离开——否则第二根抬起前手势已退回单指状态。
    // 判定后立即把这一手势标记为结束，避免另一根手指抬起时再触发一次。
    if (start?.mode === "swipe") {
      const dx = start.swipeDx;
      gesture.current = points.length > 0
        ? { startDistance: 0, startZoom: zoom, startPan: pan, startPoint: points[0], startPoints: [{ ...points[0] }], mode: "none", swipeDx: 0 }
        : null;
      if (Math.abs(dx) >= SWIPE_TRIGGER) onSwipe?.(dx < 0 ? "next" : "prev");
      return;
    }
    if (points.length === 0) {
      gesture.current = null;
      return;
    }
    // 从双指松到单指：以当前状态重新起算，否则残留的捏合基准会让图乱跳
    gesture.current = {
      startDistance: 0,
      startZoom: zoom,
      startPan: pan,
      startPoint: points[0],
      startPoints: [{ ...points[0] }],
      mode: "none",
      swipeDx: 0,
    };
  };

  const handleWheel = (event: WheelEvent<HTMLDivElement>) => {
    // 触控板双指滚动也走 wheel：Ctrl 键按下或直接滚动都按缩放处理，避免整层被页面滚走
    event.preventDefault();
    const factor = event.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP;
    applyZoom(zoom * factor);
  };

  const zoomed = zoom > MIN_ZOOM;

  return (
    <div className="relative flex size-full min-h-0 flex-col">
      <div
        role="group"
        aria-label={t("viewer_zoom_area")}
        className={cn(
          "relative flex min-h-0 flex-1 touch-none items-center justify-center overflow-hidden",
          // 放大后可拖动，光标提示可交互
          zoomed && "cursor-grab active:cursor-grabbing",
        )}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={endPointer}
        onPointerCancel={endPointer}
        onWheel={handleWheel}
      >
        {failed ? (
          <ImageOff aria-hidden className="size-10 text-muted-foreground" />
        ) : (
          <img
            src={src}
            alt={alt}
            draggable={false}
            onLoad={(event) => {
              const image = event.currentTarget;
              if (onNaturalSize && image.naturalWidth > 0 && image.naturalHeight > 0) {
                onNaturalSize({ width: image.naturalWidth, height: image.naturalHeight });
              }
            }}
            onError={() => setFailed(true)}
            className={cn("max-h-full max-w-full object-contain select-none pan-zoom", className)}
            style={{ "--zoom": zoom, "--pan-x": `${pan.x}px`, "--pan-y": `${pan.y}px` } as CSSProperties}
          />
        )}
      </div>
      {/* 控件单独占一行放在图下方：浮在图上会挡住正在看的画面，尤其在放大后。
          全屏看图（compact）不显示按钮行，倍率读数浮在右下角。 */}
      {compact ? (
        zoomed && (
          <span className="pointer-events-none absolute bottom-2 right-2 rounded-md bg-background/80 px-2 py-0.5 text-xs text-subtle-foreground tabular-nums">
            {Math.round(zoom * 100)}%
          </span>
        )
      ) : (
      <div className="flex shrink-0 items-center justify-end gap-1.5 pt-2">
        <Button
          variant="secondary"
          size="icon-sm"
          disabled={zoom <= MIN_ZOOM}
          aria-label={t("viewer_zoom_out")}
          onClick={() => applyZoom(zoom / ZOOM_STEP)}
        >
          <ZoomOut aria-hidden />
        </Button>
        <span className="min-w-12 text-center text-xs text-subtle-foreground tabular-nums">
          {Math.round(zoom * 100)}%
        </span>
        <Button
          variant="secondary"
          size="icon-sm"
          disabled={zoom >= MAX_ZOOM}
          aria-label={t("viewer_zoom_in")}
          onClick={() => applyZoom(zoom * ZOOM_STEP)}
        >
          <ZoomIn aria-hidden />
        </Button>
      </div>
      )}
    </div>
  );
}
