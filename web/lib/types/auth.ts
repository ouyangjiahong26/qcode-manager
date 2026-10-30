export type Role = 'admin' | 'viewer';

export interface Me {
  username: string;
  role: Role;
}

export interface LoginResult {
  ok: boolean;
  username: string;
  role: string;
}
