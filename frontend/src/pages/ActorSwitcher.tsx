import { UserDto } from "../api/client";
import { formatRole } from "../ui/format";

export function ActorSwitcher({
  users,
  current,
  onChange,
}: {
  users: UserDto[];
  current: string;
  onChange: (id: string) => void;
}) {
  const actor = users.find((u) => u.actor_id === current);
  return (
    <div className="actor-switcher">
      <span className="actor-label">当前演示用户</span>
      <select value={current} onChange={(e) => onChange(e.target.value)}>
        {users.map((u) => (
          <option key={u.actor_id} value={u.actor_id}>
            {u.display_name}（{u.roles.map(formatRole).join("/")}）
          </option>
        ))}
      </select>
      {actor && <span className="actor-roles">角色：{actor.roles.map(formatRole).join("、")}</span>}
    </div>
  );
}
