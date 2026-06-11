// Sample TSX component for golden snapshot testing.
"use client";

import React from "react";

interface CardProps {
  title: string;
  body: string;
}

export function Card({ title, body }: CardProps) {
  return (
    <div className="card">
      <h2>{title}</h2>
      <p>{body}</p>
    </div>
  );
}

export const EmptyCard = () => <div className="card empty" />;
