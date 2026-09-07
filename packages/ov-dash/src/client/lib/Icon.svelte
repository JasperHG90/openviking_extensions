<script lang="ts">
  import { icon } from "./icons";

  interface Props {
    name: string;
    size?: number;
  }

  const { name, size = 15 }: Props = $props();
</script>

<!-- The set is a trusted, in-repo constant, so the markup is safe to inline. -->
{@html icon(name, size)}
