<script lang="ts">
  /**
   * The editorial header every page opens with: a mono eyebrow naming the kind
   * of page, then the serif title.
   */
  interface Props {
    eyebrow: string;
    title: string;
    desc?: string;
  }

  const { eyebrow, title, desc = "" }: Props = $props();
</script>

<div class="eyebrow">{eyebrow}</div>
<h1>{title}</h1>
{#if desc}<p class="pdesc">{desc}</p>{/if}
