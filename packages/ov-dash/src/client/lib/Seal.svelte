<script lang="ts">
  /**
   * The OpenViking mark, struck as a seal.
   *
   * A longship — crescent hull, split sail — inside a ring, drawn in the
   * dashboard's ochre rather than the blue of the original wordmark, because
   * the blue belongs to a different room.
   *
   * Two rings, not one: a solid one and a hairline inside it. A single circle
   * around a logo reads as a corporate roundel; the pair reads as a stamp,
   * which is what an archive puts on the things it keeps.
   *
   * Hidden from screen readers. Wherever it is used the name is already set
   * beside it, and labelling the mark as well made every reader say
   * "OpenViking" twice.
   */

  interface Props {
    /** Rendered size in pixels. The mark is drawn in a square viewBox. */
    size?: number;
  }

  const { size = 104 }: Props = $props();
</script>

<svg
  class="seal"
  width={size}
  height={size}
  viewBox="0 0 100 100"
  aria-hidden="true"
>
  <circle class="ring" cx="50" cy="50" r="46" />
  <circle class="hair" cx="50" cy="50" r="41.5" />
  <!--
    Traced from the wordmark rather than drawn from memory: the first two
    attempts were a generic dinghy, which is what you get when you sketch "a
    boat" instead of measuring the silhouette you were given.
  -->
  <g class="ship">
    <path
      d="M44.9 26.2c0.2-1.3 2.1-1.3 2.3 0l3.1 27c0.1 0.9-0.6 1.7-1.5 1.6l-7.4-0.6c-0.9-0.1-1.5-0.9-1.3-1.8z"
    />
    <path
      d="M53.2 34c-0.4-1.2 1.2-2 2-1 5.6 7.2 11.1 15.2 14.2 24.1 0.4 1-0.4 2-1.4 1.9l-7.2-0.5c-0.7 0-1.3-0.5-1.4-1.2-1.7-8-3.8-15.8-6.2-23.3z"
    />
    <path
      d="M17.9 55.6c-1.2-0.7-0.5-2.6 0.9-2.2 14.4 4.4 29.8 4.8 44.4 1.2 1.3-0.3 2.3 1.2 1.4 2.2-5.9 6.9-14.4 11.1-23.5 11.4-8.5 0.3-16.7-2.7-22.3-8.5z"
    />
  </g>
</svg>

<style>
  .seal {
    display: block;
  }
  .ring {
    fill: none;
    stroke: var(--seal-ring);
    stroke-width: 1.25;
  }
  .hair {
    fill: none;
    stroke: var(--seal-hair);
    stroke-width: 0.75;
  }
  /* The ship's own bbox sits low and left in the square; this centres it. */
  .ship {
    fill: var(--accent);
    transform: translate(6px, 2px) scale(0.94);
    transform-origin: 50% 50%;
  }
</style>
