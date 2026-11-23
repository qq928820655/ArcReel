import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ZoomableImage } from "./ZoomableImage";

const SRC = "/files/庭院.png";

function renderImage(onNaturalSize = vi.fn()) {
  render(<ZoomableImage src={SRC} alt="「庭院」的资产图" onNaturalSize={onNaturalSize} />);
  return {
    stage: screen.getByRole("group", { name: "资产图缩放区域" }),
    image: screen.getByRole("img", { name: "「庭院」的资产图" }),
    onNaturalSize,
  };
}

/** 读当前倍率百分比，作为「缩放是否真的发生」的观测点。 */
function zoomPercent() {
  return screen.getByText(/%$/).textContent;
}

describe("ZoomableImage", () => {
  it("starts fitted to the container and reports the natural size once loaded", () => {
    const { image, onNaturalSize } = renderImage();
    expect(zoomPercent()).toBe("100%");

    // jsdom 不解码图片，手动把原图尺寸挂上去再触发 load
    Object.defineProperty(image, "naturalWidth", { value: 1672, configurable: true });
    Object.defineProperty(image, "naturalHeight", { value: 941, configurable: true });
    fireEvent.load(image);

    expect(onNaturalSize).toHaveBeenCalledWith({ width: 1672, height: 941 });
  });

  it("zooms with the buttons and clamps at both ends", async () => {
    const user = userEvent.setup();
    renderImage();

    await user.click(screen.getByRole("button", { name: "放大" }));
    expect(zoomPercent()).toBe("150%");
    await user.click(screen.getByRole("button", { name: "放大" }));
    expect(zoomPercent()).toBe("225%");

    // 缩回适应容器后不能再缩，放大按钮此时仍可用
    await user.click(screen.getByRole("button", { name: "缩小" }));
    await user.click(screen.getByRole("button", { name: "缩小" }));
    expect(zoomPercent()).toBe("100%");
    expect(screen.getByRole("button", { name: "缩小" })).toBeDisabled();

    // 上限 6 倍，到了就禁用放大
    for (let i = 0; i < 10; i += 1) {
      const zoomIn = screen.getByRole("button", { name: "放大" });
      if (zoomIn.hasAttribute("disabled")) break;
      await user.click(zoomIn);
    }
    expect(zoomPercent()).toBe("600%");
    expect(screen.getByRole("button", { name: "放大" })).toBeDisabled();
  });

  it("zooms on wheel and reports the ratio", () => {
    const { stage } = renderImage();

    fireEvent.wheel(stage, { deltaY: -100 });
    expect(zoomPercent()).toBe("150%");
    fireEvent.wheel(stage, { deltaY: 100 });
    expect(zoomPercent()).toBe("100%");
  });

  it("pans only after zooming in", () => {
    const { stage, image } = renderImage();

    // 适应容器时拖动不产生位移
    fireEvent.pointerDown(stage, { pointerId: 1, clientX: 0, clientY: 0 });
    fireEvent.pointerMove(stage, { pointerId: 1, clientX: 40, clientY: 30 });
    expect(image.style.getPropertyValue("--pan-x")).toBe("0px");
    fireEvent.pointerUp(stage, { pointerId: 1, clientX: 40, clientY: 30 });

    // 放大后同样拖动就产生位移
    fireEvent.wheel(stage, { deltaY: -100 });
    fireEvent.pointerDown(stage, { pointerId: 1, clientX: 0, clientY: 0 });
    fireEvent.pointerMove(stage, { pointerId: 1, clientX: 40, clientY: 30 });
    expect(image.style.getPropertyValue("--pan-x")).toBe("40px");
    expect(image.style.getPropertyValue("--pan-y")).toBe("30px");
  });

  it("shows a placeholder instead of the previous image when loading fails", () => {
    const { image } = renderImage();

    fireEvent.error(image);

    expect(screen.queryByRole("img", { name: "「庭院」的资产图" })).not.toBeInTheDocument();
  });

  describe("two-finger swipe", () => {
    /** 两指平行滑动：两根手指同向移动同样的距离。 */
    function swipe(stage: HTMLElement, dx: number) {
      fireEvent.pointerDown(stage, { pointerId: 1, clientX: 100, clientY: 100 });
      fireEvent.pointerDown(stage, { pointerId: 2, clientX: 180, clientY: 100 });
      fireEvent.pointerMove(stage, { pointerId: 1, clientX: 100 + dx, clientY: 100 });
      fireEvent.pointerMove(stage, { pointerId: 2, clientX: 180 + dx, clientY: 100 });
      fireEvent.pointerUp(stage, { pointerId: 1, clientX: 100 + dx, clientY: 100 });
      fireEvent.pointerUp(stage, { pointerId: 2, clientX: 180 + dx, clientY: 100 });
    }

    it("asks for the next image when both fingers move left", () => {
      const onSwipe = vi.fn();
      render(<ZoomableImage src={SRC} alt="「庭院」的资产图" onSwipe={onSwipe} />);

      swipe(screen.getByRole("group", { name: "资产图缩放区域" }), -120);

      expect(onSwipe).toHaveBeenCalledWith("next");
    });

    it("asks for the previous image when both fingers move right", () => {
      const onSwipe = vi.fn();
      render(<ZoomableImage src={SRC} alt="「庭院」的资产图" onSwipe={onSwipe} />);

      swipe(screen.getByRole("group", { name: "资产图缩放区域" }), 120);

      expect(onSwipe).toHaveBeenCalledWith("prev");
    });

    it("ignores a short drag so a stray touch does not flip the image", () => {
      const onSwipe = vi.fn();
      render(<ZoomableImage src={SRC} alt="「庭院」的资产图" onSwipe={onSwipe} />);

      swipe(screen.getByRole("group", { name: "资产图缩放区域" }), -20);

      expect(onSwipe).not.toHaveBeenCalled();
    });

    it("pinches instead of swiping when the fingers move apart", () => {
      const onSwipe = vi.fn();
      render(<ZoomableImage src={SRC} alt="「庭院」的资产图" onSwipe={onSwipe} />);
      const pinchStage = screen.getByRole("group", { name: "资产图缩放区域" });

      fireEvent.pointerDown(pinchStage, { pointerId: 1, clientX: 100, clientY: 100 });
      fireEvent.pointerDown(pinchStage, { pointerId: 2, clientX: 180, clientY: 100 });
      // 两指反向拉开：间距变大，中点不动
      fireEvent.pointerMove(pinchStage, { pointerId: 1, clientX: 60, clientY: 100 });
      fireEvent.pointerMove(pinchStage, { pointerId: 2, clientX: 220, clientY: 100 });
      fireEvent.pointerUp(pinchStage, { pointerId: 1, clientX: 60, clientY: 100 });
      fireEvent.pointerUp(pinchStage, { pointerId: 2, clientX: 220, clientY: 100 });

      expect(onSwipe).not.toHaveBeenCalled();
    });
  });
});
