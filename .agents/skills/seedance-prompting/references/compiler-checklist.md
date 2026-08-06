# Compiler Checklist

Before sending a prompt, verify:

- the provider and operation support every requested input;
- `compile_spec.surface_profile` matches provider preflight or is explicitly conservative;
- only the current clip is described;
- reference tags and roles are exact;
- identity anchors are stable and concise;
- opening state comes from accepted evidence;
- every `identity_id` resolves to one canonical registry entry;
- every temporal beat appears once, in order, with its completed state;
- one action and one camera move own each shot;
- dialogue ownership and reactions are unambiguous;
- lighting has a physical source;
- completed and reserved beats are excluded;
- the endpoint is visible and complete;
- each prompt carrier has an honest `carrier_coverage` mapping;
- reference emissions match `binding_mode`; input parameters are not emitted as tokens;
- compression decisions record what was removed and never drop the endpoint;
- required text and logos are delegated to post.
