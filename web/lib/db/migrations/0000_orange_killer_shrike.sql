CREATE TABLE `Chat` (
	`createdAt` integer NOT NULL,
	`id` text PRIMARY KEY NOT NULL,
	`title` text NOT NULL,
	`userId` text NOT NULL,
	`visibility` text DEFAULT 'private' NOT NULL,
	FOREIGN KEY (`userId`) REFERENCES `User`(`id`) ON UPDATE no action ON DELETE no action
);
--> statement-breakpoint
CREATE TABLE `Document` (
	`content` text,
	`createdAt` integer NOT NULL,
	`id` text NOT NULL,
	`text` text DEFAULT 'text' NOT NULL,
	`title` text NOT NULL,
	`userId` text NOT NULL,
	PRIMARY KEY(`id`, `createdAt`),
	FOREIGN KEY (`userId`) REFERENCES `User`(`id`) ON UPDATE no action ON DELETE no action
);
--> statement-breakpoint
CREATE TABLE `Message_v2` (
	`attachments` text NOT NULL,
	`chatId` text NOT NULL,
	`createdAt` integer NOT NULL,
	`id` text PRIMARY KEY NOT NULL,
	`parts` text NOT NULL,
	`role` text NOT NULL,
	FOREIGN KEY (`chatId`) REFERENCES `Chat`(`id`) ON UPDATE no action ON DELETE no action
);
--> statement-breakpoint
CREATE TABLE `Stream` (
	`chatId` text NOT NULL,
	`createdAt` integer NOT NULL,
	`id` text PRIMARY KEY NOT NULL,
	FOREIGN KEY (`chatId`) REFERENCES `Chat`(`id`) ON UPDATE no action ON DELETE no action
);
--> statement-breakpoint
CREATE TABLE `Suggestion` (
	`createdAt` integer NOT NULL,
	`description` text,
	`documentCreatedAt` integer NOT NULL,
	`documentId` text NOT NULL,
	`id` text PRIMARY KEY NOT NULL,
	`isResolved` integer DEFAULT false NOT NULL,
	`originalText` text NOT NULL,
	`suggestedText` text NOT NULL,
	`userId` text NOT NULL,
	FOREIGN KEY (`userId`) REFERENCES `User`(`id`) ON UPDATE no action ON DELETE no action,
	FOREIGN KEY (`documentId`,`documentCreatedAt`) REFERENCES `Document`(`id`,`createdAt`) ON UPDATE no action ON DELETE no action
);
--> statement-breakpoint
CREATE TABLE `User` (
	`createdAt` integer NOT NULL,
	`email` text(64) NOT NULL,
	`emailVerified` integer DEFAULT false NOT NULL,
	`id` text PRIMARY KEY NOT NULL,
	`image` text,
	`isAnonymous` integer DEFAULT false NOT NULL,
	`name` text,
	`password` text(64),
	`updatedAt` integer NOT NULL
);
--> statement-breakpoint
CREATE TABLE `Vote_v2` (
	`chatId` text NOT NULL,
	`isUpvoted` integer NOT NULL,
	`messageId` text NOT NULL,
	PRIMARY KEY(`chatId`, `messageId`),
	FOREIGN KEY (`chatId`) REFERENCES `Chat`(`id`) ON UPDATE no action ON DELETE no action,
	FOREIGN KEY (`messageId`) REFERENCES `Message_v2`(`id`) ON UPDATE no action ON DELETE no action
);
